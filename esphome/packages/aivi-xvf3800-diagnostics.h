// AIVI satellite: XVF3800 diagnostics helpers
//
// Used by aivi-xvf3800-diagnostics.yaml. Reads and writes XMOS control
// servicers over I2C with status handling, and samples the levels of the lines
// between the ESP32 and the XVF3800.
#pragma once

#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>

#include "driver/gpio.h"

#include "esphome/components/respeaker_xvf3800/respeaker_xvf3800.h"

namespace aivi_diagnostics {

using Xvf = esphome::respeaker_xvf3800::RespeakerXVF3800;

// Status bytes of the XMOS control transport, plus one of our own for a bus
// error. The servicers on the audio tile answer "wait" or "retry" until they
// have processed a command, and "queue full" when commands pile up.
constexpr uint8_t kStatusDone = 0x00;
constexpr uint8_t kStatusWait = 0x01;
constexpr uint8_t kStatusRetry = 0x40;
constexpr uint8_t kStatusBusError = 0xFE;
// 30 tries with 3 ms pauses: a running audio pipeline answers within one or
// two 15 ms frames.
constexpr uint8_t kMaxTries = 30;

inline bool busy(uint8_t status) { return status == kStatusWait || status == kStatusRetry; }

// Control read: write {resid, cmd | 0x80, len + 1}, then read a status byte and
// len payload bytes. Repeats the request while the servicer is busy.
inline uint8_t read(Xvf *xvf, uint8_t resid, uint8_t cmd, uint8_t *out, uint8_t len, uint8_t &tries) {
  if (len > 32)
    return kStatusBusError;
  const uint8_t request[3] = {resid, static_cast<uint8_t>(cmd | 0x80), static_cast<uint8_t>(len + 1)};
  uint8_t response[33];
  uint8_t status = kStatusBusError;
  for (tries = 1;; tries++) {
    std::memset(response, 0, sizeof(response));
    if (xvf->write_read(request, sizeof(request), response, len + 1) != esphome::i2c::ERROR_OK)
      return kStatusBusError;
    status = response[0];
    if (!busy(status) || tries >= kMaxTries)
      break;
    esphome::delay(3);
  }
  std::memcpy(out, response + 1, len);
  return status;
}

// Control write: {resid, cmd, len, payload...}, then read the status byte back.
// Repeats the command while the servicer is busy.
inline uint8_t write(Xvf *xvf, uint8_t resid, uint8_t cmd, const uint8_t *data, uint8_t len) {
  if (len > 32)
    return kStatusBusError;
  uint8_t request[35] = {resid, cmd, len};
  std::memcpy(request + 3, data, len);
  uint8_t status = kStatusBusError;
  for (uint8_t tries = 1; tries <= kMaxTries; tries++) {
    if (xvf->write(request, len + 3) != esphome::i2c::ERROR_OK)
      return kStatusBusError;
    esphome::delay(5);
    if (xvf->read(&status, 1) != esphome::i2c::ERROR_OK)
      return kStatusBusError;
    if (!busy(status))
      break;
    esphome::delay(10);
  }
  return status;
}

// Write without reading a status back, for commands that reset the chip.
inline bool send(Xvf *xvf, uint8_t resid, uint8_t cmd, const uint8_t *data, uint8_t len) {
  if (len > 32)
    return false;
  uint8_t request[35] = {resid, cmd, len};
  std::memcpy(request + 3, data, len);
  return xvf->write(request, len + 3) == esphome::i2c::ERROR_OK;
}

// --- Probes: one control read per call, round robin ---

struct Probe {
  uint8_t resid;
  uint8_t cmd;
  uint8_t len;
  const char *name;
};

// Command ids from Seeed's XVF3800 I2C command list. The first three live on
// the control tile, all others on the audio tile.
inline const Probe kProbes[] = {
    {240, 88, 3, "dfu_version"},  {20, 0, 5, "gpo_values"},   {48, 0, 3, "app_version"},
    {33, 80, 16, "aec_spenergy"}, {33, 75, 16, "aec_azimuth"}, {33, 3, 4, "aec_converged"},
    {35, 0, 4, "am_mic_gain"},    {35, 2, 4, "am_idle_time"},  {35, 15, 2, "am_op_left"},
    {35, 19, 2, "am_op_right"},   {17, 10, 4, "pp_agc_on"},    {17, 13, 4, "pp_agc_gain"},
};
constexpr size_t kProbeCount = sizeof(kProbes) / sizeof(kProbes[0]);

struct ProbeResult {
  uint8_t status{kStatusBusError};
  uint8_t tries{0};
  bool seen{false};
  uint8_t data[16]{};
};

inline ProbeResult probe_results[kProbeCount];
inline size_t probe_next_index = 0;

// Runs the next probe and returns its log line.
inline std::string probe_next(Xvf *xvf) {
  const size_t index = probe_next_index;
  probe_next_index = (probe_next_index + 1) % kProbeCount;
  const Probe &probe = kProbes[index];
  ProbeResult &result = probe_results[index];
  uint8_t data[16] = {0};
  result.status = read(xvf, probe.resid, probe.cmd, data, probe.len, result.tries);
  result.seen = true;
  std::memcpy(result.data, data, probe.len);
  char hex[3 * 16 + 1] = {0};
  for (uint8_t i = 0; i < probe.len; i++)
    snprintf(hex + 3 * i, 4, "%02x ", data[i]);
  char line[160];
  snprintf(line, sizeof(line), "probe %-13s %3u/%-3u status=0x%02x tries=%-2u data=%s", probe.name, probe.resid,
           probe.cmd, result.status, result.tries, hex);
  return line;
}

// The result of a probe that answered "done", or nullptr.
inline const ProbeResult *probe_ok(const char *name) {
  for (size_t i = 0; i < kProbeCount; i++) {
    if (std::strcmp(kProbes[i].name, name) == 0)
      return probe_results[i].seen && probe_results[i].status == kStatusDone ? &probe_results[i] : nullptr;
  }
  return nullptr;
}

inline float probe_float(const char *name, size_t index = 0) {
  const ProbeResult *result = probe_ok(name);
  if (result == nullptr)
    return NAN;
  float value;
  std::memcpy(&value, result->data + 4 * index, sizeof(value));
  return value;
}

inline float probe_int32(const char *name) {
  const ProbeResult *result = probe_ok(name);
  if (result == nullptr)
    return NAN;
  int32_t value;
  std::memcpy(&value, result->data, sizeof(value));
  return static_cast<float>(value);
}

// Largest speech energy of the four beams; above zero while someone speaks.
inline float speech_energy() {
  float best = NAN;
  for (size_t beam = 0; beam < 4; beam++) {
    const float value = probe_float("aec_spenergy", beam);
    if (!std::isnan(value) && (std::isnan(best) || value > best))
      best = value;
  }
  return best;
}

// "category/source" of an output mux probe, or empty.
inline std::string probe_mux(const char *name) {
  const ProbeResult *result = probe_ok(name);
  if (result == nullptr)
    return "";
  return std::to_string(result->data[0]) + "/" + std::to_string(result->data[1]);
}

// Status of every probe run so far, e.g. "dfu_version=00 aec_spenergy=40".
inline std::string probe_summary() {
  std::string text;
  for (size_t i = 0; i < kProbeCount; i++) {
    if (!probe_results[i].seen)
      continue;
    char item[32];
    snprintf(item, sizeof(item), "%s%s=%02x", text.empty() ? "" : " ", kProbes[i].name, probe_results[i].status);
    text += item;
  }
  return text;
}

// --- Pin probe ---

// Share of high samples on a pin, polled asynchronously. A running clock reads
// about half high, audio data some, a line held low none.
inline uint32_t high_count(gpio_num_t pin, uint32_t samples) {
  uint32_t high = 0;
  for (uint32_t i = 0; i < samples; i++)
    high += gpio_get_level(pin);
  return high;
}

inline std::string pin_config(gpio_num_t pin) {
  gpio_io_config_t config{};
  if (gpio_get_io_config(pin, &config) != ESP_OK)
    return "?";
  char text[40];
  snprintf(text, sizeof(text), "pu=%d pd=%d ie=%d oe=%d", config.pu, config.pd, config.ie, config.oe);
  return text;
}

inline std::string pin_text() {
  constexpr uint32_t kSamples = 4000;
  char text[240];
  snprintf(text, sizeof(text), "d43 %s high=%u | bclk %u | lrclk %u | g9 %s high=%u | g44 %s high=%u (of %u)",
           pin_config(GPIO_NUM_43).c_str(), (unsigned) high_count(GPIO_NUM_43, kSamples),
           (unsigned) high_count(GPIO_NUM_8, kSamples), (unsigned) high_count(GPIO_NUM_7, kSamples),
           pin_config(GPIO_NUM_9).c_str(), (unsigned) high_count(GPIO_NUM_9, kSamples),
           pin_config(GPIO_NUM_44).c_str(), (unsigned) high_count(GPIO_NUM_44, kSamples), (unsigned) kSamples);
  return text;
}

}  // namespace aivi_diagnostics
