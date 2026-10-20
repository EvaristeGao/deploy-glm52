// adxl/HIXL 层最小复现（Device↔Device / Device↔Host 传输，不依赖 mooncake / vLLM）
//
// 目的：复现 vllm-ascend AscendStore(layerwise) 里那条失败链：
//   Endpoint CreateChannel failed → HixlClient Connect failed →
//   rtsIpcMemImportByKey(107000) / halDeviceDisableP2P(17) → 传输失败后 Segfault
// 本 demo 直接用 CANN 自带的 adxl::AdxlEngine（mooncake 的 ascend_transport.so 也是 ldd 到
// libllm_datadist.so 的同一套实现），跑 Device↔Device / Device↔Host × 不同 GlobalResourceConfig。
//
// 编译（容器内 CANN 9.1.0）：
//   g++ -std=c++17 -O2 adxl_xfer_demo.cc \
//     -I/usr/local/Ascend/cann-9.1.0/aarch64-linux/include \
//     -L/usr/local/Ascend/cann-9.1.0/aarch64-linux/lib64 \
//     -lllm_datadist -lascendcl -o adxl_xfer_demo
//
// 用法（两个进程，可跨节点，也可同机）：
//   ./adxl_xfer_demo server --ip 172.168.4.237 --port 24001 --mem dev  --size-kb 1024 --hold 25
//   ./adxl_xfer_demo client --ip 172.168.4.237 --peer 172.168.4.237:24001 --mem dev --size-kb 1024
//   # 可选：--protocol roce|ubmem|fabric   （fabric 会额外加 EnableUseFabricMem=1）
//   # 可选：--autoconnect 1|0（默认 1，对应引擎里的 ASCEND_AUTO_CONNECT=0/1）
//
// 判据：server 端 hold 结束后打印 "SERVER_RECV first=0x.. last=0x.."，
//   0xcd = 客户端写成功；rc != 0 时打印 adxl 返回码（103900=PARAM_INVALID、503900=FAILED）。
#include <acl/acl.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include <unistd.h>

#include "graph/ascend_string.h"
#include "adxl/adxl_types.h"
#include "adxl/adxl_engine.h"

namespace {

constexpr unsigned char kServerPattern = 0xAB;
constexpr unsigned char kClientPattern = 0xCD;

void Usage() {
  std::printf(
      "usage:\n"
      "  server --ip I --port P [--mem dev|host] [--size-kb N] [--hold S] [--protocol ...] [--device D]\n"
      "  client --ip I --peer I:P [--mem dev|host] [--size-kb N] [--protocol ...] [--device D]\n"
      "protocol: default | roce | ubmem | fabric\n");
}

std::string Arg(int argc, char** argv, const char* name, const char* def = nullptr) {
  for (int i = 2; i + 1 < argc; ++i) {
    if (std::strcmp(argv[i], name) == 0) return argv[i + 1];
  }
  return def ? std::string(def) : std::string();
}

bool HasFlag(int argc, char** argv, const char* name) {
  for (int i = 2; i < argc; ++i) {
    if (std::strcmp(argv[i], name) == 0) return true;
  }
  return false;
}

std::string GlobalResourceConfig(const std::string& protocol) {
  if (protocol == "roce") {
    return R"({"comm_resource_config":{"protocol_desc":"roce:device"}})";
  }
  if (protocol == "ubmem") {
    return R"({"comm_resource_config":{"protocol_desc":"ubmem"}})";
  }
  if (protocol == "fabric") {
    // fabric_memory.max_capacity 单位是 TB，范围 (0,1024]
    return R"({"comm_resource_config":{"protocol_desc":"ubmem"},"fabric_memory":{"max_capacity":1,"task_stream_num":4}})";
  }
  return "";
}

const char* MemTypeName(adxl::MemType t) { return t == adxl::MEM_DEVICE ? "device" : "host"; }

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    Usage();
    return 1;
  }
  const std::string role = argv[1];
  const bool is_server = (role == "server");
  const std::string ip = Arg(argc, argv, "--ip", "127.0.0.1");
  const int port = std::atoi(Arg(argc, argv, "--port", "0").c_str());
  const std::string peer = Arg(argc, argv, "--peer");
  const std::string mem = Arg(argc, argv, "--mem", "dev");
  const size_t size = static_cast<size_t>(std::atoll(Arg(argc, argv, "--size-kb", "1024").c_str())) << 10;
  const int hold_sec = std::atoi(Arg(argc, argv, "--hold", "25").c_str());
  const std::string protocol = Arg(argc, argv, "--protocol", "default");
  const std::string autoconnect = Arg(argc, argv, "--autoconnect", "1");
  const int device = std::atoi(Arg(argc, argv, "--device", "0").c_str());
  const std::string alloc = Arg(argc, argv, "--alloc", "acl");  // acl = aclrtMalloc/malloc；vmm = AdxlEngine::MallocMem
  const adxl::MemType mtype = (mem == "host") ? adxl::MEM_HOST : adxl::MEM_DEVICE;

  aclError acl_rc = aclInit(nullptr);
  if (acl_rc != ACL_SUCCESS) {
    std::printf("[acl] aclInit rc=%d\n", static_cast<int>(acl_rc));
    return 2;
  }
  acl_rc = aclrtSetDevice(device);
  if (acl_rc != ACL_SUCCESS) {
    std::printf("[acl] aclrtSetDevice(%d) rc=%d\n", device, static_cast<int>(acl_rc));
    return 2;
  }

  // 客户端也给一个自己的监听端口（adxl 的 local_engine 标识必须带 port，否则 Connect 报 PARAM_INVALID）
  const std::string local = ip + ":" + std::to_string(port > 0 ? port : (is_server ? 24001 : 24002));
  std::map<adxl::AscendString, adxl::AscendString> options;
  options[adxl::OPTION_AUTO_CONNECT] = autoconnect.c_str();
  const std::string grc = GlobalResourceConfig(protocol);
  if (!grc.empty()) options["GlobalResourceConfig"] = grc.c_str();
  if (protocol == "fabric") options["EnableUseFabricMem"] = "1";

  adxl::AdxlEngine engine;
  uint32_t rc = engine.Initialize(local.c_str(), options);
  std::printf("[init] role=%s local=%s mem=%s size=%zu protocol=%s autoconnect=%s Initialize rc=%u\n",
              role.c_str(), local.c_str(), MemTypeName(mtype), size, protocol.c_str(),
              autoconnect.c_str(), rc);
  if (rc != 0) {
    std::printf("RESULT FAIL (Initialize rc=%u)\n", rc);
    return 3;
  }
  if (!grc.empty()) std::printf("[init] GlobalResourceConfig=%s\n", grc.c_str());

  void* p = nullptr;
  if (alloc == "vmm") {
    rc = adxl::AdxlEngine::MallocMem(mtype, size, &p);
    std::printf("[mem] AdxlEngine::MallocMem(%s, %zu) rc=%u ptr=%p\n", MemTypeName(mtype), size, rc, p);
  } else {
    // 默认走普通分配：device 用 aclrtMalloc，host 用 aclrtMallocHost
    aclError arc = (mtype == adxl::MEM_DEVICE)
                       ? aclrtMalloc(&p, size, ACL_MEM_MALLOC_HUGE_FIRST)
                       : aclrtMallocHost(&p, size);
    rc = (arc == ACL_SUCCESS) ? 0 : static_cast<uint32_t>(arc);
    std::printf("[mem] %s(%s, %zu) rc=%u ptr=%p\n",
                (mtype == adxl::MEM_DEVICE) ? "aclrtMalloc" : "aclrtMallocHost",
                MemTypeName(mtype), size, rc, p);
  }
  if (rc != 0 || p == nullptr) {
    std::printf("RESULT FAIL (MallocMem rc=%u)\n", rc);
    return 3;
  }
  std::vector<unsigned char> host(size, is_server ? kServerPattern : kClientPattern);
  if (mtype == adxl::MEM_DEVICE) {
    acl_rc = aclrtMemcpy(p, size, host.data(), size, ACL_MEMCPY_HOST_TO_DEVICE);
  } else {
    std::memcpy(p, host.data(), size);
    acl_rc = ACL_SUCCESS;
  }
  if (acl_rc != ACL_SUCCESS) {
    std::printf("[mem] fill rc=%d\n", static_cast<int>(acl_rc));
    return 3;
  }

  adxl::MemHandle handle = nullptr;
  adxl::MemDesc desc{reinterpret_cast<uintptr_t>(p), size};
  rc = engine.RegisterMem(desc, mtype, handle);
  std::printf("[mem] RegisterMem rc=%u handle=%p\n", rc, handle);
  if (rc != 0) {
    std::printf("RESULT FAIL (RegisterMem rc=%u)\n", rc);
    return 3;
  }

  if (is_server) {
    std::printf("[server] listening, sleep %ds\n", hold_sec);
    std::fflush(stdout);
    sleep(hold_sec);
    std::vector<unsigned char> got(size, 0);
    if (mtype == adxl::MEM_DEVICE) {
      aclrtMemcpy(got.data(), size, p, size, ACL_MEMCPY_DEVICE_TO_HOST);
    } else {
      std::memcpy(got.data(), p, size);
    }
    const bool ok = (got[0] == kClientPattern) && (got[size - 1] == kClientPattern);
    std::printf("SERVER_RECV first=0x%02x last=0x%02x (expect 0x%02x)\n", got[0], got[size - 1],
                kClientPattern);
    std::printf("RESULT %s\n", ok ? "PASS" : "FAIL (server buffer unchanged)");
    engine.DeregisterMem(handle);
    if (alloc == "vmm") {
      adxl::AdxlEngine::FreeMem(p);
    } else if (mtype == adxl::MEM_DEVICE) {
      aclrtFree(p);
    } else {
      aclrtFreeHost(p);
    }
    engine.Finalize();
    aclrtResetDevice(device);
    aclFinalize();
    return ok ? 0 : 4;
  }

  rc = engine.Connect(peer.c_str(), 10000);
  std::printf("[client] Connect(%s) rc=%u\n", peer.c_str(), rc);
  if (rc != 0) {
    std::printf("RESULT FAIL (Connect rc=%u)\n", rc);
    return 3;
  }
  std::vector<adxl::TransferOpDesc> ops;
  ops.push_back(adxl::TransferOpDesc{reinterpret_cast<uintptr_t>(p), 0 /* remote offset */, size});
  rc = engine.TransferSync(peer.c_str(), adxl::WRITE, ops, 10000);
  std::printf("[client] TransferSync(WRITE, len=%zu) rc=%u\n", size, rc);
  std::printf("RESULT %s\n", rc == 0 ? "PASS" : "FAIL (TransferSync)");
  engine.DeregisterMem(handle);
  adxl::AdxlEngine::FreeMem(p);
  engine.Finalize();
  aclrtResetDevice(device);
  aclFinalize();
  return rc == 0 ? 0 : 4;
}
