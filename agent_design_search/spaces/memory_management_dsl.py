"""Memory-management DSL primitives: reservation and whole-object migration.

Logical lifetimes stay fixed; execution and timing are implemented separately.
"""

from dataclasses import dataclass

from agent_design_search.contracts import CandidateError


@dataclass(frozen=True)
class AllocationPolicy:
    reserve_on: tuple

    @classmethod
    def parse(cls, obj, objects):
        if (not isinstance(obj, dict) or set(obj) != {"version", "reserve_on"}
                or type(obj["version"]) is not int or obj["version"] != 1):
            raise CandidateError("invalid_allocation_policy", "$.dsl", "Expected version=1 and reserve_on")
        triggers = obj["reserve_on"]
        if not isinstance(triggers, dict) or set(triggers) != set(objects):
            raise CandidateError("invalid_allocation_policy", "$.dsl.reserve_on", "Name every workload object exactly once")
        for name, trigger in triggers.items():
            if trigger not in ("create", "first_access"):
                raise CandidateError("invalid_allocation_policy", f"$.dsl.reserve_on.{name}",
                                     "Reservation trigger must be create or first_access")
        return cls(tuple(sorted(triggers.items())))

    def to_dict(self):
        return {"version": 1, "reserve_on": dict(self.reserve_on)}


@dataclass(frozen=True)
class MigrationPolicy:
    rules: tuple  # (object, trigger, destination); never has no destination.

    @classmethod
    def parse(cls, obj, objects, options):
        if (not isinstance(obj, dict) or set(obj) != {"version", "objects"}
                or type(obj["version"]) is not int or obj["version"] != 1):
            raise CandidateError("invalid_migration_policy", "$.dsl", "Expected version=1 and objects")
        rules = obj["objects"]
        if not isinstance(rules, dict) or set(rules) != set(objects):
            raise CandidateError("invalid_migration_policy", "$.dsl.objects", "Name every workload object exactly once")
        parsed = []
        for name, rule in sorted(rules.items()):
            path = f"$.dsl.objects.{name}"
            if not isinstance(rule, dict) or type(rule.get("trigger")) is not str:
                raise CandidateError("invalid_migration_policy", path, "Expected an object with trigger")
            trigger = rule["trigger"]
            if trigger == "never":
                if set(rule) != {"trigger"}:
                    raise CandidateError("invalid_migration_policy", path, "never takes no destination")
                destination = None
            else:
                if (set(rule) != {"trigger", "destination"} or trigger not in options["triggers"]
                        or type(rule["destination"]) is not str or rule["destination"] not in options["destinations"]):
                    raise CandidateError("invalid_migration_policy", path, "Use an allowed trigger and destination")
                destination = rule["destination"]
            parsed.append((name, trigger, destination))
        return cls(tuple(parsed))

    def to_dict(self):
        return {"version": 1, "objects": {name: {"trigger": trigger,
                **({"destination": destination} if destination is not None else {})}
                for name, trigger, destination in self.rules}}
