#pragma once

#ifdef USE_ESP32

#include "analysis.h"
#include "xvf3800_control.h"

#include "esphome/components/microphone/microphone_source.h"
#include "esphome/components/respeaker_xvf3800/respeaker_xvf3800.h"
#include "esphome/components/ring_buffer/ring_buffer.h"
#include "esphome/components/speaker/speaker.h"

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/core/defines.h"
#include "esphome/core/preferences.h"

#ifdef USE_OTA_STATE_LISTENER
#include "esphome/components/ota/ota_backend.h"
#endif

#include <atomic>
#include <memory>
#include <string>

namespace esphome::aec_tuning {

/// Echo canceller settings of the XVF3800 and the self test (issue #17).
///
/// AUDIO_MGR_SYS_DELAY and AUDIO_MGR_REF_GAIN are kept in the ESP32's flash
/// and written at boot, because the XVF3800 forgets them on a restart. Until
/// someone sets them, the XVF3800 keeps its own defaults.
///
/// The self test runs the engine of analysis.h: it plays the test signal
/// through the given speaker (a mixer input), reads the XVF3800's output
/// channels from the microphone and carries out the engine's control writes.
/// The microphone callback only copies audio into a PSRAM ring buffer; the
/// analysis runs in loop().
class AecTuning final : public Component
#ifdef USE_OTA_STATE_LISTENER
    ,
                        public ota::OTAGlobalStateListener
#endif
{
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  // After the XVF3800 driver.
  float get_setup_priority() const override { return setup_priority::LATE; }

#ifdef USE_OTA_STATE_LISTENER
  void on_ota_global_state(ota::OTAState state, float progress, uint8_t error, ota::OTAComponent *comp) override;
#endif

  void set_respeaker(respeaker_xvf3800::RespeakerXVF3800 *respeaker) { this->respeaker_ = respeaker; }
  void set_speaker(speaker::Speaker *speaker) { this->speaker_ = speaker; }
  void set_microphone_source(microphone::MicrophoneSource *source) { this->microphone_source_ = source; }

  /// Writes AUDIO_MGR_SYS_DELAY (samples, -64..256) and keeps it for the
  /// next boot. False while a self test runs or if the write fails.
  bool set_sys_delay(float value);
  /// Writes AUDIO_MGR_REF_GAIN and keeps it for the next boot.
  bool set_ref_gain(float value);
  /// Current values; NAN until read from the XVF3800.
  float sys_delay() const { return this->sys_delay_; }
  float ref_gain() const { return this->ref_gain_; }
  /// AEC_AECCONVERGED, read every 2 s: 1, 0, or -1 while unknown.
  int converged() const { return this->converged_; }

  /// Starts the self test, or with attenuation_only the attenuation test,
  /// which changes no setting; false if one runs or the parts are missing.
  bool start_self_test(bool attenuation_only = false);
  /// Stops a running self test; the saved settings come back.
  void abort_self_test();
  bool self_test_running() const { return this->testing_; }
  const aec_self_test::Results &results() const { return this->engine_.results(); }
  const char *error_code() const { return aec_self_test::Engine::error_code(this->engine_.error()); }

  /// Fires with "running:<step>", "done" or "error:<code>".
  Trigger<std::string> *get_status_trigger() { return &this->status_trigger_; }

 protected:
  struct SavedTuning {
    int32_t sys_delay;
    float ref_gain;
    uint8_t flags;  // kSavedSysDelay | kSavedRefGain
  };
  static constexpr uint8_t kSavedSysDelay = 1 << 0;
  static constexpr uint8_t kSavedRefGain = 1 << 1;

  void boot_tuning_();
  void poll_converged_();
  void run_self_test_();
  void run_action_();
  void feed_speaker_();
  void read_input_();
  void finish_self_test_();
  void save_tuning_();
  void report_(const std::string &status);

  respeaker_xvf3800::RespeakerXVF3800 *respeaker_{nullptr};
  speaker::Speaker *speaker_{nullptr};
  microphone::MicrophoneSource *microphone_source_{nullptr};

  ESPPreferenceObject pref_;
  SavedTuning saved_{0, 0.0f, 0};

  bool tuning_ready_{false};
  uint32_t next_tuning_try_ms_{0};
  float sys_delay_{NAN};
  float ref_gain_{NAN};
  int converged_{-1};
  uint32_t last_poll_ms_{0};

  aec_self_test::Engine engine_;
  bool testing_{false};
  float volume_before_{1.0f};  // codec volume before the test
  uint32_t test_started_ms_{0};
  aec_self_test::Step reported_step_{aec_self_test::Step::IDLE};
  int16_t *engine_buffer_{nullptr};
  std::unique_ptr<ring_buffer::RingBuffer> ring_buffer_;
  std::atomic<bool> capturing_{false};
  std::atomic<uint32_t> dropped_bytes_{0};

  // Settings before the test, restored when it fails.
  bool before_valid_{false};
  aivi_xvf3800::Mux mux_left_before_{};
  aivi_xvf3800::Mux mux_right_before_{};
  int32_t sys_delay_before_{0};
  float ref_gain_before_{NAN};

  static constexpr size_t kOutSamples = 1024;
  int16_t out_[kOutSamples]{};
  size_t out_bytes_{0};
  size_t out_offset_{0};
  static constexpr size_t kInFrames = 512;
  int16_t in_[2 * kInFrames]{};

  Trigger<std::string> status_trigger_;
};

}  // namespace esphome::aec_tuning

#endif  // USE_ESP32
