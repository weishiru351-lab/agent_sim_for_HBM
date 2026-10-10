// Fixed-arrival, per-channel backpressure bridge. No address remapping here.
// Read-only: upstream write callbacks currently mean WR issue/coalescing, not
// data completion, and must not silently inherit the read completion contract.
#include <algorithm>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#include "ramulator/base/config.h"
#include "ramulator/base/factory.h"
#include "ramulator/frontend/i_frontend.h"
#include "ramulator/memory_system/i_memory_system.h"

using Ramulator::ConfigNode;

struct Transaction {
  long long id, request_id, arrival, address;
  Ramulator::AddrVec_t coordinates;
  long long accepted = -1, finish = -1;
};

static std::string quote(const std::string& text) {
  std::string result = "\"";
  for (unsigned char c : text) {
    if (c == '"' || c == '\\') result += '\\';
    if (c == '\n') result += "\\n";
    else if (c == '\r') result += "\\r";
    else if (c == '\t') result += "\\t";
    else if (c < 32) throw std::runtime_error("Unsupported control character in statistics");
    else result += static_cast<char>(c);
  }
  return result + '"';
}

static void json(std::ostream& out, const ConfigNode& node) {
  if (node.is_map()) {
    out << '{'; bool first = true;
    for (const auto& [key, value] : node.map()) {
      if (!first) out << ','; first = false;
      out << quote(key) << ':'; json(out, value);
    }
    out << '}';
  } else if (node.is_sequence()) {
    out << '['; bool first = true;
    for (const auto& value : node.seq()) {
      if (!first) out << ','; first = false; json(out, value);
    }
    out << ']';
  } else if (node.is_null()) out << "null";
  else out << quote(node.scalar());  // upstream stat scalars retain exact text
}

int main(int argc, char** argv) {
  try {
    if (argc != 3) throw std::runtime_error("Usage: hbm_ramulator bundle.json native_result.json");
    auto bundle = Ramulator::Config::parse_config_file(argv[1]);
    if (bundle["protocol"].as<std::string>() != "hbm-ramulator-read-v1" ||
        bundle["upstream_commit"].as<std::string>() != RAMULATOR_REVISION)
      throw std::runtime_error("Unsupported bundle protocol or upstream revision");
    auto native = bundle["ramulator_config"];
    const auto channels = native["memory_system"]["controllers"].size();
    if (channels == 0 || channels > 8) throw std::runtime_error("Invalid channel count");
    if (native["memory_system"]["channel_mapper"]["impl"].as<std::string>() != "PassThroughChannelMapper")
      throw std::runtime_error("Channel mapper must preserve compiled coordinates");
    for (const auto& ctrl : native["memory_system"]["controllers"].seq()) {
      if (ctrl["addr_mapper"]["impl"].as<std::string>() != "PassThroughAddrMapper" ||
          ctrl["dram"]["impl"].as<std::string>() != "HBM2")
        throw std::runtime_error("Expected HBM2 and pass-through address mapper");
    }
    const auto max_cycles = bundle["max_cycles"].as<long long>();
    std::vector<Transaction> transactions;
    for (const auto& item : bundle["transactions"].seq()) {
      if (item["kind"].as<std::string>() != "read" || item["size_bytes"].as<int>() != 32)
        throw std::runtime_error("This driver accepts only 32-byte reads");
      Transaction tx{item["transaction_id"].as<long long>(), item["request_id"].as<long long>(),
                     item["arrival"].as<long long>(), item["physical_address"].as<long long>(), {}};
      for (const auto& coordinate : item["addr_vec"].seq()) tx.coordinates.push_back(coordinate.as<int>());
      if (tx.id != static_cast<long long>(transactions.size()) || tx.arrival < 0 || tx.arrival >= max_cycles || tx.address < 0 || tx.address % 32)
        throw std::runtime_error("Invalid transaction identity, time or address");
      const int bounds[] = {static_cast<int>(channels), 2, 1, 4, 4, 16384, 32};
      if (tx.coordinates.size() != 7) throw std::runtime_error("HBM2 needs seven coordinates");
      for (int i = 0; i < 7; ++i)
        if (tx.coordinates[i] < 0 || tx.coordinates[i] >= bounds[i]) throw std::runtime_error("Address coordinate out of range");
      transactions.push_back(tx);
    }
    if (transactions.empty() || max_cycles <= 0) throw std::runtime_error("Empty trace or invalid cycle limit");

    auto* frontend = Ramulator::Factory::create_frontend(native);
    auto* memory = Ramulator::Factory::create_memory_system(native);
    frontend->connect_memory_system(memory);
    memory->connect_frontend(frontend);
    if (memory->get_tx_bytes() != 32) throw std::runtime_error("Unexpected DRAM transaction size");
    std::vector<std::vector<size_t>> queues(channels);
    for (size_t i = 0; i < transactions.size(); ++i) queues[transactions[i].coordinates[0]].push_back(i);
    for (auto& queue : queues) std::stable_sort(queue.begin(), queue.end(), [&](size_t a, size_t b) {
      return transactions[a].arrival < transactions[b].arrival;
    });
    std::vector<size_t> cursors(channels, 0);
    size_t completed = 0, accepted = 0;
    long long clock = 0, rejected_attempts = 0;
    while (completed < transactions.size()) {
      if (clock >= max_cycles) throw std::runtime_error("Cycle limit exceeded before every read completed");
      for (size_t channel = 0; channel < channels; ++channel) {
        auto& cursor = cursors[channel];
        while (cursor < queues[channel].size()) {
          const auto index = queues[channel][cursor];
          auto& tx = transactions[index];
          if (tx.arrival > clock) break;
          Ramulator::Request request(tx.address, Ramulator::Request::Type::Read, 0,
            [&, index](Ramulator::Request& done) {
              auto& record = transactions[index];
              if (record.accepted < 0 || record.finish >= 0 || done.addr_vec != record.coordinates || done.depart > clock || done.depart < record.accepted)
                throw std::runtime_error("Invalid/duplicate callback or address was remapped");
              record.finish = done.depart;
              ++completed;
            });
          request.addr_vec = tx.coordinates;
          request.size_bytes = 32;
          if (!memory->send(request)) { ++rejected_attempts; break; }
          tx.accepted = clock;
          ++accepted; ++cursor;
        }
      }
      ++clock;
      memory->tick();
    }
    memory->update_stats_recursive();
    std::ofstream out(argv[2]);
    if (!out) throw std::runtime_error("Cannot create result file");
    out << "{\"backend\":\"ramulator-2.1-hbm2-read-v1\",\"upstream_commit\":" << quote(RAMULATOR_REVISION)
        << ",\"bundle_id\":" << quote(bundle["bundle_id"].as<std::string>())
        << ",\"accepted_transactions\":" << accepted << ",\"completed_transactions\":" << completed
        << ",\"final_tick\":" << clock << ",\"tCK_ns\":" << memory->get_tCK()
        << ",\"rejected_send_attempts\":" << rejected_attempts << ",\"events\":[";
    bool first = true;
    for (const auto& tx : transactions) {
      if (!first) out << ','; first = false;
      out << "{\"transaction_id\":" << tx.id << ",\"request_id\":" << tx.request_id
          << ",\"arrival\":" << tx.arrival << ",\"accepted\":" << tx.accepted << ",\"finish\":" << tx.finish << '}';
    }
    out << "],\"controller_stats\":"; json(out, memory->collect_stats()); out << "}\n";
    frontend->finalize(); memory->finalize();
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n'; return 1;
  }
}
