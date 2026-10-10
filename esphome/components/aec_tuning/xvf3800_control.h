// AIVI satellite: XVF3800 control over I2C
//
// Reads and writes XMOS control servicers with status handling. Used by the
// aec_tuning component and by the diagnostics package
// (esphome/packages/aivi-xvf3800-diagnostics.h).
#pragma once

#include <cstdint>
#include <cstring>

#include "esphome/components/respeaker_xvf3800/respeaker_xvf3800.h"
#include "esphome/core/hal.h"

namespace aivi_xvf3800 {

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

// Resource and command ids from XMOS XVF3800 v3.2.1 (control command appendix)
// and Seeed's xvf_host.py. Values are little endian.
constexpr uint8_t kAecResid = 33;
constexpr uint8_t kAecConverged = 3;  // int32, read only, 0 or 1
constexpr uint8_t kAudioMgrResid = 35;
constexpr uint8_t kAudioMgrRefGain = 1;    // float, pre-SHF reference gain
constexpr uint8_t kAudioMgrOpLeft = 15;    // uint8 category, uint8 source
constexpr uint8_t kAudioMgrOpRight = 19;   // uint8 category, uint8 source
constexpr uint8_t kAudioMgrSysDelay = 26;  // int32 samples at 16 kHz, -64..256

// Output mux categories (XMOS tuning guide).
constexpr uint8_t kMuxMicPostGain = 3;
constexpr uint8_t kMuxRefPostGain = 5;
constexpr uint8_t kMuxAecResidual = 7;

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

// Typed helpers; true when the servicer answered "done".

inline bool read_int32(Xvf *xvf, uint8_t resid, uint8_t cmd, int32_t &value) {
  uint8_t data[4];
  uint8_t tries;
  if (read(xvf, resid, cmd, data, sizeof(data), tries) != kStatusDone)
    return false;
  std::memcpy(&value, data, sizeof(value));
  return true;
}

inline bool write_int32(Xvf *xvf, uint8_t resid, uint8_t cmd, int32_t value) {
  uint8_t data[4];
  std::memcpy(data, &value, sizeof(data));
  return write(xvf, resid, cmd, data, sizeof(data)) == kStatusDone;
}

inline bool read_float(Xvf *xvf, uint8_t resid, uint8_t cmd, float &value) {
  uint8_t data[4];
  uint8_t tries;
  if (read(xvf, resid, cmd, data, sizeof(data), tries) != kStatusDone)
    return false;
  std::memcpy(&value, data, sizeof(value));
  return true;
}

inline bool write_float(Xvf *xvf, uint8_t resid, uint8_t cmd, float value) {
  uint8_t data[4];
  std::memcpy(data, &value, sizeof(data));
  return write(xvf, resid, cmd, data, sizeof(data)) == kStatusDone;
}

struct Mux {
  uint8_t category{0};
  uint8_t source{0};
};

inline bool read_mux(Xvf *xvf, uint8_t cmd, Mux &mux) {
  uint8_t data[2];
  uint8_t tries;
  if (read(xvf, kAudioMgrResid, cmd, data, sizeof(data), tries) != kStatusDone)
    return false;
  mux.category = data[0];
  mux.source = data[1];
  return true;
}

inline bool write_mux(Xvf *xvf, uint8_t cmd, Mux mux) {
  const uint8_t data[2] = {mux.category, mux.source};
  return write(xvf, kAudioMgrResid, cmd, data, sizeof(data)) == kStatusDone;
}

}  // namespace aivi_xvf3800
