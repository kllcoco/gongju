#pragma once
#include <cstdint>
#include <span>
#include <string>
namespace recovery::snapshot {
// Operates on caller-owned snapshots only; never dereferences a QQ address.
uint64_t field(std::span<const uint8_t> records,uint32_t id);
uint32_t integer(uint64_t tagged);
uint64_t uid_address(uint64_t tagged);
std::string uid(std::span<const uint8_t> encoded);
}
