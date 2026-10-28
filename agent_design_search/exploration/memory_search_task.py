"""One frozen memory search: inputs, allowed strategy changes and execution.

All current spaces compose here. There is no per-space task/measurement stack.
"""

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from math import lcm

from agent_design_search.contracts import CandidateError, Objective
from agent_design_search.inputs.hardware import Hardware, MemorySystem
from agent_design_search.inputs.workload import KVWorkload, parse_trace, statistics
from agent_design_search.json_io import digest, read
from agent_design_search.execution import memory_system as execution
from agent_design_search.verification.memory_contents import verify_contents
from agent_design_search.performance.dram import FCFSParameters, simulate
from agent_design_search.performance.nand import NANDParameters
from agent_design_search.performance.ssd import BlockParameters, SSDInterfaceParameters
from agent_design_search.spaces.dram_dsl import Policy, verify_mapping
from agent_design_search.spaces.kv_layout_dsl import Layout, LayoutBounds, KVLayout, PlacedLayout
from agent_design_search.spaces.memory_management_dsl import AllocationPolicy, MigrationPolicy
from agent_design_search.spaces.nand_dsl import parse_page_mapping, parse_gc_policy


class MemorySearchTask:
    def __init__(self, spec):
        spec = deepcopy(spec)
        required = {"version", "workload", "hardware", "execution", "design_space", "evaluation"}
        if (not isinstance(spec, dict) or not required <= set(spec)
                or set(spec) - required - {"fixed_design", "initial_design", "budget"}
                or type(spec["version"]) is not int or spec["version"] not in (1, 2, 3)):
            raise ValueError("Search requires version=1, 2 or 3, workload, hardware, execution, design_space, evaluation")
        self.system = MemorySystem(spec["hardware"]) if spec["version"] == 3 else None
        self.hardware = self.system or Hardware.parse(spec["hardware"])
        self.budget = spec.get("budget", 20)
        if type(self.budget) is not int or self.budget < 1:
            raise ValueError("budget must be a positive integer")
        source = spec["workload"]
        if not isinstance(source, dict) or set(source) != {"kind", "data"}:
            raise ValueError("Resolved workload needs kind and data")
        self.kind = source["kind"]
        if self.kind == "kv_events":
            self.workload, self.requests = KVWorkload.parse(source["data"]), ()
            source["data"] = self.workload.to_dict()
            design_keys = {"kv_layout", "dram_address_mapping"}
            if self.system and not self.system.mapping_geometries:
                design_keys.remove("dram_address_mapping")
        elif self.kind == "address_trace":
            if self.system:
                raise ValueError("Topology execution currently requires kv_events; traces have no target identity")
            self.workload, self.requests = None, tuple(parse_trace(source["data"], self.hardware))
            provenance = source["data"].get("provenance")
            source["data"] = {"version": 1, "requests": [asdict(r) for r in self.requests]}
            if provenance is not None:
                source["data"]["provenance"] = provenance
            design_keys = {"dram_address_mapping"}
        else:
            raise ValueError("workload.kind must be kv_events or address_trace")
        self.lifecycle = self.workload is not None and self.workload.version == 3
        if self.lifecycle and not self.system:
            raise ValueError("Lifecycle workloads require an explicit topology task (version=3)")
        mode = spec["execution"]
        expected_mode = "serial_groups" if self.workload else "fixed_arrivals"
        mode_keys = ({"kind", "allocation", "migration"} if self.lifecycle else
                     {"kind", "base_address"} if self.workload and not self.system else {"kind"})
        self.nand_targets = self.system.nand_targets if self.system else {}
        self.nand_design_targets = self.system.nand_design_targets if self.system else {}
        if self.nand_targets:
            mode_keys.add("nand")
        if (not isinstance(mode, dict) or mode.get("kind") != expected_mode
                or set(mode) - mode_keys):
            raise ValueError(f"This workload requires execution.kind={expected_mode}")
        self.nand_execution = mode.get("nand")
        if self.system and self.system.has_ssd_nand and not self.nand_execution:
            raise ValueError("Detailed SSD requires explicit execution.nand")
        if "nand" in mode:
            if (not isinstance(self.nand_execution, dict) or not {"kind", "buffer_pages"} <= set(self.nand_execution)
                    or set(self.nand_execution) - {"kind", "buffer_pages", "gc"}
                    or self.nand_execution["kind"] != "channel_die"
                    or type(self.nand_execution["buffer_pages"]) is not int or not 1 <= self.nand_execution["buffer_pages"] <= 64):
                raise ValueError("execution.nand needs kind=channel_die and fixed buffer_pages (1..64 per target)")
            if "gc" in self.nand_execution and (
                    self.nand_execution["gc"] not in ("foreground_greedy", "policy")
                    or any(t.geometry.planes_per_die * t.geometry.blocks_per_plane < 2 for t in self.nand_targets.values())):
                raise ValueError("execution.nand.gc must be foreground_greedy or policy, with at least two blocks per die")
            if any(t.management_bytes < t.metadata_bytes + self.nand_execution["buffer_pages"] * t.geometry.page_bytes
                   + (8 * t.geometry.blocks if self.nand_execution.get("gc") == "policy" and n in self.nand_design_targets else 0)
                   for n, t in self.nand_targets.items()):
                raise ValueError("Every NAND management budget must cover metadata and the declared page buffers")
            if self.nand_design_targets:
                design_keys.add("nand_page_mapping")
            if self.nand_execution.get("gc") == "policy":
                if not self.nand_design_targets:
                    raise ValueError("GC policy search needs an open NAND controller")
                design_keys.add("nand_gc")
        if self.lifecycle and mode.get("allocation") not in ("group_first_fit", "policy_first_fit"):
            raise ValueError("Lifecycle execution requires allocation=group_first_fit or policy_first_fit")
        self.allocation_policy = self.lifecycle and mode["allocation"] == "policy_first_fit"
        self.migration = mode.get("migration")
        self.migration_policy = isinstance(self.migration, dict) and self.migration.get("kind") == "policy"
        if "migration" in mode:
            if (not self.allocation_policy or not isinstance(self.migration, dict)
                    or set(self.migration) != ({"kind", "buffer_bytes"} if self.migration_policy else {"on_full", "buffer_bytes"})
                    or type(self.migration["buffer_bytes"]) is not int or not 1 <= self.migration["buffer_bytes"] <= 1048576):
                raise ValueError("Migration requires policy_first_fit, kind=policy or on_full pairs, and buffer_bytes (1..1 MiB)")
            if not self.migration_policy and (not isinstance(self.migration["on_full"], dict) or not self.migration["on_full"]
                    or not set(self.migration["on_full"]) <= set(self.workload.shapes)
                    or any(type(r) is not str for r in self.migration["on_full"].values())):
                raise ValueError("Fixed on_full must name workload objects and destination regions")
        if self.allocation_policy:
            design_keys.add("allocation_policy")
        if self.migration_policy:
            design_keys.add("migration_policy")
        self.base_address = mode.get("base_address", 0)
        if not self.system and (type(self.base_address) is not int or not 0 <= self.base_address < self.hardware.capacity_bytes
                                  or self.base_address % self.hardware.granule_bytes):
            raise ValueError("base_address must be aligned and inside hardware capacity")
        self.bounds = {}
        self._default_design = {"dram_address_mapping": ({"version": 1, "groups": {
            n: Policy().to_dict() for n in self.system.mapping_geometries}} if self.system else Policy().to_dict())}
        if "dram_address_mapping" not in design_keys:
            self._default_design = {}
        if self.allocation_policy:
            self._default_design["allocation_policy"] = {"version": 1, "reserve_on": dict.fromkeys(self.workload.shapes, "create")}
        if self.migration_policy:
            self._default_design["migration_policy"] = {"version": 1, "objects": {n: {"trigger": "never"} for n in self.workload.shapes}}
        space = spec["design_space"]
        if not isinstance(space, dict) or "vary" not in space or set(space) - {"vary", "mapping_family", "max_mapping_operations", "layout_options", "migration_options", "nand_options", "gc_options"}:
            raise ValueError("design_space needs vary and supported mapping/layout/migration/NAND options")
        self.nand_stripe_pages = []
        if "nand_page_mapping" in design_keys:
            options = space.get("nand_options")
            if (not isinstance(options, dict) or set(options) != {"stripe_pages"}
                    or not isinstance(options["stripe_pages"], list) or not options["stripe_pages"]
                    or any(type(n) is not int or not 1 <= n <= 1 << 24 for n in options["stripe_pages"])
                    or len(set(options["stripe_pages"])) != len(options["stripe_pages"])):
                raise ValueError("nand_options needs distinct positive stripe_pages choices (1..2^24)")
            self.nand_stripe_pages = sorted(options["stripe_pages"])
            self._default_design["nand_page_mapping"] = {"version": 1, "targets": {n: {
                "axis_order": ["channel", "die"], "stripe_pages": self.nand_stripe_pages[0]} for n in self.nand_design_targets}}
        elif "nand_options" in space:
            raise ValueError("nand_options requires channel_die execution and an open NAND controller")
        self.gc_options = {}
        if "nand_gc" in design_keys:
            options = space.get("gc_options")
            limit = min(t.geometry.planes_per_die * t.geometry.blocks_per_plane for t in self.nand_design_targets.values())
            if (not isinstance(options, dict) or set(options) != {"trigger_free_blocks", "victims"}
                    or any(not isinstance(v, list) or not v for v in options.values())
                    or any(type(v) is not int or not 1 <= v < limit for v in options["trigger_free_blocks"])
                    or any(type(v) is not str or v not in ("most_invalid", "oldest_reclaimable") for v in options["victims"])
                    or any(len(set(v)) != len(v) for v in options.values())):
                raise ValueError("gc_options requires distinct legal trigger_free_blocks and victims choices")
            self.gc_options = {k: sorted(v) for k, v in options.items()}
            self._default_design["nand_gc"] = {"version": 1, "targets": {n: {
                "trigger_free_blocks": self.gc_options["trigger_free_blocks"][0], "victim": self.gc_options["victims"][0]}
                for n in self.nand_design_targets}}
        elif "gc_options" in space:
            raise ValueError("gc_options requires execution.nand.gc=policy")
        self.migration_options = {}
        if self.migration_policy:
            options = space.get("migration_options")
            if (not isinstance(options, dict) or set(options) != {"triggers", "destinations"}
                    or any(not isinstance(v, list) or not v or any(type(x) is not str for x in v)
                           or len(set(v)) != len(v) for v in options.values())
                    or not set(options["triggers"]) <= {"on_full", "before_read"}):
                raise ValueError("migration_options needs distinct nonempty destinations and triggers (on_full/before_read); never is always allowed")
            self.migration_options = {k: sorted(v) for k, v in options.items()}
        elif "migration_options" in space:
            raise ValueError("migration_options requires execution.migration.kind=policy")
        varying = space["vary"]
        if (not isinstance(varying, list) or not varying or any(type(v) is not str for v in varying)
                or len(set(varying)) != len(varying) or not set(varying) <= design_keys):
            raise ValueError("vary must name available spaces once; address traces cannot search layouts")
        self.vary = frozenset(varying)
        self.mapping_family = space.get("mapping_family", "linear")
        self.max_mapping_operations = space.get("max_mapping_operations", 8)
        if (self.mapping_family not in ("linear", "permutation")
                or type(self.max_mapping_operations) is not int or not 0 <= self.max_mapping_operations <= 128):
            raise ValueError("Invalid mapping family or operation limit (0..128)")
        self._configure_layout(space, mode)
        fixed = spec.get("fixed_design", {})
        if not isinstance(fixed, dict) or not set(fixed) <= design_keys - self.vary:
            raise ValueError("fixed_design may only specify available, non-varying spaces")
        self._default_design.update(fixed)
        self._fixed = {}
        if "initial_design" in spec:
            # A feasible explicit seed is useful for unequal capacities; no hidden packing search.
            self._default_design = self.normalize(spec["initial_design"])
            for name, value in fixed.items():
                if self._default_design[name] != self.normalize({**self._default_design, name: value})[name]:
                    raise ValueError("initial_design must respect fixed_design")
        self._default_design = self.normalize(self._default_design)
        self._fixed = {k: v for k, v in self._default_design.items() if k not in self.vary}
        self._configure_evaluation(spec["evaluation"])
        self._spec = {"version": 3 if self.system else 2, "workload": source, "hardware": self.hardware.to_dict(), "execution": mode,
                      "design_space": {"vary": sorted(self.vary), "mapping_family": self.mapping_family,
                                       "max_mapping_operations": self.max_mapping_operations,
                                       **({"migration_options": self.migration_options} if self.migration_policy else {}),
                                       **({"nand_options": {"stripe_pages": self.nand_stripe_pages}} if self.nand_stripe_pages else {}),
                                       **({"gc_options": self.gc_options} if self.gc_options else {}),
                                       **({"layout_options": self.layout_options} if self.workload else {})},
                      "fixed_design": self._fixed, "budget": self.budget,
                      "evaluation": {"backend": self.backend, "objective": self.objective.metric,
                                     "parameters": asdict(self.parameters) if self.parameters else {}}}
        if "initial_design" in spec:
            self._spec["initial_design"] = self.default_design
        if self.nand_parameters:
            self._spec["evaluation"]["nand_parameters"] = {n: asdict(p) for n, p in self.nand_parameters.items()}
        if self.ssd_parameters:
            self._spec["evaluation"]["ssd_parameters"] = {n: asdict(p) for n, p in self.ssd_parameters.items()}
        if self.allocation_parameters:
            self._spec["evaluation"]["allocation_parameters"] = dict(self.allocation_parameters)
        if self.migration_parameters:
            self._spec["evaluation"]["migration_parameters"] = dict(self.migration_parameters)

    def _configure_layout(self, space, mode):
        """Set allowed placement sizes and the initial object layout."""
        self.layout_options = {}
        if self.workload:
            if self.system:
                options = space.get("layout_options", {})
                self.pool = options.get("pool") if isinstance(options, dict) else None
                if not isinstance(self.pool, str) or self.pool not in self.system.pools:
                    raise ValueError("Topology layout_options must select one declared pool")
                regions = self.system.pools[self.pool]
                granules = [self.system.targets[self.system.regions[r].target].geometry.granule_bytes for r in regions]
                line = lcm(*granules, *(shape.element_bytes for _, shape in self.workload.objects))
                capacity = sum(self.system.regions[r].size_bytes for r in regions)
                self.layout_options["pool"] = self.pool
                if self.migration:
                    destinations = self.migration_options["destinations"] if self.migration_policy else self.migration["on_full"].values()
                    if any(r not in regions for r in destinations) or self.migration["buffer_bytes"] % line:
                        raise ValueError("Migration destinations must belong to the layout pool; buffer_bytes must be a common transfer multiple")
            else:
                line, capacity = self.hardware.granule_bytes, self.hardware.capacity_bytes - self.base_address
                options = space.get("layout_options", {"alignment_bytes": [line], "chunk_bytes": [line]})
                mode["base_address"] = self.base_address
            fields = {"alignment_bytes", "chunk_bytes"} | ({"pool"} if self.system else set())
            if not isinstance(options, dict) or set(options) != fields:
                raise ValueError("layout_options needs alignment_bytes and chunk_bytes lists, plus pool for topology")
            self.bounds = {name: LayoutBounds(**asdict(shape), line_bytes=line, capacity_bytes=capacity)
                           for name, shape in self.workload.objects}
            for key in ("alignment_bytes", "chunk_bytes"):
                values = options[key]
                if (not isinstance(values, list) or not values
                        or any(type(v) is not int or not 1 <= v <= capacity or v % line for v in values)
                        or len(set(values)) != len(values)):
                    raise ValueError("Layout sizes must be distinct positive transfer multiples within capacity")
                if key == "alignment_bytes" and any(self.base_address % v for v in values):
                    raise ValueError("Fixed base_address must satisfy all allowed object alignments")
                self.layout_options[key] = sorted(values)
            self._default_design["kv_layout"] = KVLayout(
                tuple((n, Layout(1, b.dimensions)) for n, b in self.bounds.items()), tuple(self.bounds),
                alignment_bytes=self.layout_options["alignment_bytes"][0]).to_dict()
            if self.system:
                local = self._default_design["kv_layout"]
                self._default_design["kv_layout"] = {"version": 3, "objects": local["objects"], "groups": [
                    {"composition": local["composition"], "placement": {"regions": list(regions),
                     "stripe_bytes": self.layout_options["chunk_bytes"][0]}}]}
                if self.lifecycle:
                    template = self._default_design["kv_layout"]["groups"][0]
                    self._default_design["kv_layout"]["groups"] = [
                        {"composition": {**local["composition"], "order": [name]}, "placement": deepcopy(template["placement"])}
                        for name in self.bounds]
        elif "layout_options" in space:
            raise ValueError("Address traces cannot configure KV layout options")

    def _configure_evaluation(self, evaluation):
        """Select the compatible backend and validate its fixed timing costs."""
        if (not isinstance(evaluation, dict) or not {"backend", "objective"} <= set(evaluation)
                or set(evaluation) - {"backend", "objective", "parameters", "nand_parameters", "ssd_parameters", "allocation_parameters", "migration_parameters"}):
            raise ValueError("evaluation needs backend, objective and supported timing parameter groups")
        self.backend = evaluation["backend"]
        system_backend = "fcfs-hybrid-v1" if self.system and self.system.has_nand else "fcfs-system-v1"
        if self.system and self.system.has_ssd:
            system_backend = "fcfs-block-system-v1"
        self.allocation_parameters = None
        if self.lifecycle:
            system_backend = "fcfs-allocation-v1" if self.allocation_policy else "fcfs-lifecycle-v1"
            self.allocation_parameters = evaluation.get("allocation_parameters")
            fields = {"create_ps", "invalidate_ps"} | ({"reserve_ps", "release_ps"} if self.allocation_policy else set())
            if (not isinstance(self.allocation_parameters, dict) or set(self.allocation_parameters) != fields
                    or any(type(v) is not int or v < 1 for v in self.allocation_parameters.values())):
                raise ValueError(f"allocation_parameters requires positive integer costs: {', '.join(sorted(fields))}")
        elif "allocation_parameters" in evaluation:
            raise ValueError("Static workloads cannot configure unused allocation_parameters")
        self.migration_parameters = evaluation.get("migration_parameters")
        if self.migration:
            system_backend = "fcfs-migration-policy-v1" if self.migration_policy else "fcfs-migration-v1"
            if (not isinstance(self.migration_parameters, dict)
                    or set(self.migration_parameters) != {"publish_ps", "copy_ps_per_byte"}
                    or any(type(v) is not int or v < 1 for v in self.migration_parameters.values())):
                raise ValueError("migration_parameters requires positive publish_ps and copy_ps_per_byte")
        elif "migration_parameters" in evaluation:
            raise ValueError("Migration is disabled; migration_parameters would be unused")
        if self.nand_execution:
            system_backend = ("fcfs-nand-gc-policy-v1" if self.gc_options else
                              "fcfs-nand-gc-v1" if self.nand_execution.get("gc") else "fcfs-nand-policy-v1")
        if self.system and self.system.has_ssd_nand:
            system_backend = "fcfs-ssd-nand-policy-v1" if self.gc_options else "fcfs-ssd-nand-v1"
        expected_fcfs = system_backend if self.system else ("fcfs-serial-kv-v2" if self.workload else "toy-fcfs-v1")
        if self.backend == "fcfs":
            self.backend = expected_fcfs
        if self.workload and self.backend == "fcfs-serial-kv-v1":
            self.backend = "fcfs-serial-kv-v2"
        if self.backend == "line-traffic-v1":
            self.backend = "line-traffic-v2"
        parameters = evaluation.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError("evaluation.parameters must be an object")
        nand = set(self.nand_targets)
        self.nand_parameters = {}
        if nand:
            configs = evaluation.get("nand_parameters")
            if not isinstance(configs, dict) or set(configs) != nand:
                raise ValueError("nand_parameters must explicitly configure every NAND target, and only those targets")
            try:
                self.nand_parameters = {n: NANDParameters(**cfg) for n, cfg in configs.items()}
            except TypeError as error:
                raise ValueError("NAND timing needs read/program/erase/transfer/lookup/merge/bind_ps") from error
        elif "nand_parameters" in evaluation:
            raise ValueError("No NAND target to evaluate; nand_parameters would be ignored")
        ssd = {n for n, t in self.system.targets.items() if t.medium == "ssd"} if self.system else set()
        self.ssd_parameters = {}
        if ssd:
            configs = evaluation.get("ssd_parameters")
            if not isinstance(configs, dict) or set(configs) != ssd:
                raise ValueError("ssd_parameters must explicitly configure every SSD target, and only those targets")
            try:
                self.ssd_parameters = {n: (SSDInterfaceParameters(**cfg) if self.system.targets[n].nand else BlockParameters(**cfg))
                                       for n, cfg in configs.items()}
            except TypeError as error:
                raise ValueError("SSD timing: abstract needs read/write/queue_depth/merge; detailed needs command/completion/merge_ps") from error
        elif "ssd_parameters" in evaluation:
            raise ValueError("No SSD target to evaluate; ssd_parameters would be ignored")
        if self.backend == "line-traffic-v2":
            if self.system or not self.workload or "dram_address_mapping" in self.vary or parameters:
                raise ValueError("line-traffic-v2 supports KV layout only; it cannot score DRAM mapping or timing")
            if self._default_design["dram_address_mapping"] != Policy().to_dict():
                raise ValueError("line-traffic-v2 requires identity mapping (routing is not evaluated)")
            self.parameters, metric = None, "transferred_bytes"
        elif self.backend == expected_fcfs:
            try:
                self.parameters = FCFSParameters(**parameters)
            except TypeError as error:
                raise ValueError("Unknown FCFS parameter") from error
            if self.system and not self.system.mapping_geometries:
                if parameters:
                    raise ValueError("Tasks without DRAM cannot configure DRAM timing parameters")
                self.parameters = None
            metric = "completion_ps" if self.system else "completion_cycles"
        else:
            raise ValueError("Unsupported workload/backend combination; Ramulator is not integrated")
        if evaluation["objective"] != metric:
            raise ValueError(f"This execution profile requires objective={metric}")
        self.objective = Objective(metric)

    @classmethod
    def from_file(cls, path):
        """Resolve references relative to search.json, then freeze their full content."""
        path = Path(path)
        spec = read(path)
        if not isinstance(spec, dict):
            raise ValueError("Search file must be an object")
        if isinstance(spec.get("hardware"), str):
            spec["hardware"] = read(path.parent / spec["hardware"])
        source = spec.get("workload")
        if isinstance(source, dict) and set(source) == {"kind", "file"}:
            spec["workload"] = {"kind": source["kind"], "data": read(path.parent / source["file"])}
        return cls(spec)

    @property
    def default_design(self):
        return deepcopy(self._default_design)

    def snapshot(self):
        return deepcopy(self._spec)

    @property
    def definition(self):
        return {"domain": "memory", "version": self._spec["version"], "input_id": digest(self._spec), "backend": self.backend}

    def normalize(self, candidate):
        if not isinstance(candidate, dict) or set(candidate) != set(self._default_design):
            raise CandidateError("invalid_design", "$.dsl", "Submit a complete design with exactly the available spaces",
                                 expected=sorted(self._default_design))
        result = {}
        for name in self._default_design:
            try:
                if name == "dram_address_mapping":
                    mappings = self.parse_mappings(candidate[name])
                    result[name] = ({"version": 1, "groups": {n: p.to_dict() for n, p in mappings.items()}}
                                    if self.system else mappings["default"].to_dict())
                elif name == "allocation_policy":
                    result[name] = AllocationPolicy.parse(candidate[name], self.bounds).to_dict()
                elif name == "migration_policy":
                    result[name] = MigrationPolicy.parse(candidate[name], self.bounds, self.migration_options).to_dict()
                elif name == "nand_page_mapping":
                    result[name] = parse_page_mapping(candidate[name], self.nand_design_targets, self.nand_stripe_pages)
                elif name == "nand_gc":
                    result[name] = parse_gc_policy(candidate[name], self.nand_design_targets, self.gc_options)
                else:
                    result[name] = self.parse_layout(candidate[name]).to_dict()
            except CandidateError as error:
                raise CandidateError(error.code, error.path.replace("$.dsl", f"$.dsl.{name}", 1),
                                     str(error), **error.details) from error
            if name in self._fixed and result[name] != self._fixed[name]:
                raise CandidateError("fixed_strategy", f"$.dsl.{name}", "This strategy is fixed in the search task")
        if self.migration:
            self.validate_migration_layout(self.parse_layout(result["kv_layout"]), self.migration_plan(result))
        return result

    def migration_plan(self, candidate):
        """Resolve fixed input or candidate rules into the same execution contract."""
        if not self.migration:
            return None
        rules = (candidate["migration_policy"]["objects"] if self.migration_policy else
                 {n: {"trigger": "on_full", "destination": r} for n, r in self.migration["on_full"].items()})
        return {"rules": {n: dict(rule) for n, rule in rules.items() if rule["trigger"] != "never"},
                "buffer_bytes": self.migration["buffer_bytes"]}

    def validate_migration_layout(self, layout, plan):
        for i, (group, regions, stripe) in enumerate(layout.groups):
            for name in group.order:
                if name not in plan["rules"]:
                    continue
                destination = plan["rules"][name]["destination"]
                path = f"$.dsl.kv_layout.groups[{i}]"
                if len(group.order) != 1 or len(regions) != 1 or destination in regions:
                    raise CandidateError("unsupported_migration_layout", path,
                                         "Migration needs a singleton, one source region and a distinct destination", object=name)
                if self.system.regions[destination].base_address % stripe:
                    raise CandidateError("invalid_alignment", path, "Destination must preserve stripe alignment", object=name)

    def parse_layout(self, obj):
        if self.system:
            return PlacedLayout.parse(obj, self.bounds, self.layout_options["alignment_bytes"],
                                        self.layout_options["chunk_bytes"], self.system, self.pool, dynamic=self.lifecycle)
        return KVLayout.parse(obj, self.bounds, self.layout_options["alignment_bytes"], self.layout_options["chunk_bytes"])

    def parse_mappings(self, obj):
        geometries = self.system.mapping_geometries if self.system else {"default": self.hardware}
        if not geometries and obj is None:
            return {}
        if self.system:
            if (not isinstance(obj, dict) or set(obj) != {"version", "groups"}
                    or type(obj["version"]) is not int or obj["version"] != 1
                    or not isinstance(obj["groups"], dict) or set(obj["groups"]) != set(geometries)):
                raise CandidateError("invalid_mapping_groups", "$.dsl", "Use exactly the hardware's fixed mapping groups")
            obj = obj["groups"]
        else:
            obj = {"default": obj}
        result = {}
        for name, geometry in geometries.items():
            try:
                policy = Policy.parse(obj[name], geometry, self.max_mapping_operations)
                if self.mapping_family == "permutation" and any(op[0] != "swap_bits" for op in policy.operations):
                    raise CandidateError("outside_space", "$.dsl.operations", "This task permits swaps only")
                result[name] = policy
            except CandidateError as error:
                path = error.path.replace("$.dsl", "$.dsl.groups." + name, 1) if self.system else error.path
                raise CandidateError(error.code, path, str(error), **error.details) from error
        return result

    def candidate_id(self, candidate):
        mappings = self.parse_mappings(candidate.get("dram_address_mapping"))
        identity = [self.definition, ({n: p.masks(self.system.mapping_geometries[n]) for n, p in mappings.items()}
                                     if self.system else mappings["default"].masks(self.hardware))]
        if "nand_page_mapping" in candidate:
            identity.append(candidate["nand_page_mapping"])
        if self.gc_options:
            identity.append(candidate["nand_gc"])
        if self.workload:
            layout = self.parse_layout(candidate["kv_layout"])
            if self.lifecycle:
                # Group boundaries/alignment affect reuse and lifetimes, even with identical local addresses.
                identity.append(layout.to_dict())
                if self.allocation_policy:
                    identity.append(candidate["allocation_policy"])
                if self.migration_policy:
                    identity.append(candidate["migration_policy"])
                return digest(identity)
            identity += [layout.offsets(self.bounds), layout.allocated_bytes(self.bounds)]
            if self.system:
                identity.append(layout.reservations(self.bounds)[1])
        return digest(identity)

    def check(self, candidate):
        mappings = self.parse_mappings(candidate.get("dram_address_mapping"))
        geometries = self.system.mapping_geometries if self.system else {"default": self.hardware}
        for name, mapping in mappings.items():
            result = verify_mapping(mapping, geometries[name])
            if result["status"] != "pass":
                return {**result, "mapping_group": name}
        if not self.workload:
            return result
        layout = self.parse_layout(candidate["kv_layout"])
        mapping = mappings if self.system else mappings["default"]
        reserve_on = candidate["allocation_policy"]["reserve_on"] if self.allocation_policy else None
        migration = self.migration_plan(candidate)
        if migration:
            self.validate_migration_layout(layout, migration)
        offsets, allocator = execution.prepare_layout(self.workload, layout, self.bounds, self.hardware, reserve_on)
        transfers = execution.kv_transfers(self.workload, offsets, self.hardware, mapping, self.base_address, allocator, migration)
        try:
            return verify_contents(self.workload, layout, self.bounds, self.hardware, mapping, self.base_address, transfers, reserve_on, migration,
                                   candidate.get("nand_page_mapping", {}).get("targets"),
                                   self.nand_execution["buffer_pages"] if self.nand_execution else 1,
                                   self.nand_execution.get("gc") if self.nand_execution else None,
                                   candidate["nand_gc"]["targets"] if self.gc_options else None)
        finally:
            transfers.close()

    def measure(self, candidate, include_events=False):
        mappings = self.parse_mappings(candidate.get("dram_address_mapping"))
        mapping = mappings if self.system else mappings["default"]
        if not self.workload:
            return simulate(execution.route_requests(self.requests, mapping, self.hardware),
                            self.hardware, self.parameters, include_events)
        layout, migration = self.parse_layout(candidate["kv_layout"]), self.migration_plan(candidate)
        if migration:
            self.validate_migration_layout(layout, migration)
        return execution.execute_kv(self.workload, self.bounds, layout,
                                    mapping, self.hardware, self.base_address, self.backend, self.parameters, include_events,
                                    self.nand_parameters, self.ssd_parameters, self.allocation_parameters,
                                    candidate["allocation_policy"]["reserve_on"] if self.allocation_policy else None,
                                    migration, self.migration_parameters,
                                    candidate.get("nand_page_mapping", {}).get("targets"), self.nand_execution,
                                    candidate["nand_gc"]["targets"] if self.gc_options else None)

    def feedback(self):
        return {"input_kind": self.kind, "hardware": self.hardware.to_dict(), "vary": sorted(self.vary),
                "fixed_design": deepcopy(self._fixed), "initial_design": deepcopy(self._default_design),
                "workload": self.workload.summary() if self.workload else statistics(self.requests, self.hardware),
                "objective": asdict(self.objective), "backend": self.backend,
                "limits": ("Candidate foreground GC; no background GC, wear model, power-loss recovery or calibrated timing"
                           if self.gc_options else
                           "Fixed foreground GC; no background GC, wear model, power-loss recovery or calibrated timing"
                           if self.nand_execution and self.nand_execution.get("gc") else
                           "Fixed logical lifetimes, first-fit, last-member release and optional whole-object migration; no GC or calibrated timing"
                           if self.lifecycle else "Fixed workload/execution; static placement and mapping; no data cache, migration, numerical attention or calibrated medium timing"),
                **({"nand_controller": ("Candidate striping, foreground GC threshold/victim; one block reserve; oldest_reclaimable tracks first invalidation since erase; age metadata and all actions are charged"
                                        if self.gc_options else
                                        "Candidate striping; fixed foreground greedy GC within each die, one block capacity reserve; copy/publish/erase and target-wide barriers are charged"
                                        if self.nand_execution and self.nand_execution.get("gc") else
                                        "Candidate channel/die striping, lowest legal page within die, bounded buffers and shared channel buses; planes share a die; no auto GC"
                                        if self.nand_execution else "Fixed ascending physical pages, out-of-place updates, one page scratch buffer, serial per target, no auto GC")}
                   if self.nand_parameters else {}),
                **({"ssd_adapter": ("Detailed SSD: sequential host commands, shared NAND page engine/GC; closed controllers use fixed striping/GC; no TRIM, wear or power-loss recovery"
                                   if self.system.has_ssd_nand else
                                   "Host LBA only; one block per command; serial RMWs before other group requests; charged links/merge; no FTL or persistence guarantee")}
                   if self.ssd_parameters else {}),
                **({"allocation": ("Per-object create/first_access triggers; first trigger reserves the whole group; charged reservation/release; "
                                    if self.allocation_policy else "Fixed group first-fit at create; ")
                                   + "release after last live member; generation-tagged content; NAND unbind without erase, no SSD TRIM"}
                   if self.lifecycle else {}),
                **({"migration": deepcopy(self.migration),
                    "migration_limits": "At most one move per generation, after full initialization; migrating objects need singleton single-region groups; unchanged layout, drained chunk copies, no concurrent access"}
                   if self.migration else {}),
                "dsl": {**({"kv_layout": ("per-object tile/order; object groups; concat/interleave; region selection and striping"
                                         if self.system else "per-object tile/order; concat or chunk interleave within one fixed DRAM region"),
                         "layout_options": deepcopy(self.layout_options)} if self.workload else {}),
                        **({"nand_gc": "version=1; targets selects trigger_free_blocks and victim; one-block capacity reserve is fixed",
                            "gc_options": deepcopy(self.gc_options)} if self.gc_options else {}),
                        **({"dram_address_mapping": "swap_bits/xor_bit within the declared DRAM mapping groups"}
                           if "dram_address_mapping" in self._default_design else {}),
                        **({"allocation_policy": "version=1; reserve_on names every object with create or first_access; never changes workload events or target selection"}
                           if self.allocation_policy else {}),
                        **({"migration_policy": "version=1; objects names every object: trigger=never, or trigger=on_full/before_read with destination; before_read means first read AFTER full initialization; no later read means no move",
                            "migration_options": deepcopy(self.migration_options)} if self.migration_policy else {}),
                        **({"nand_page_mapping": "version=1; targets names every NAND target with axis_order (channel/die fastest first) and stripe_pages; logical page number is target-relative; no fallback when a selected die is full",
                            "nand_options": {"stripe_pages": self.nand_stripe_pages}, "nand_execution": deepcopy(self.nand_execution)} if self.nand_stripe_pages else {}),
                        "max_mapping_operations": self.max_mapping_operations, "mapping_family": self.mapping_family}}
