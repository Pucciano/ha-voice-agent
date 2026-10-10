#include "aec_tuning.h"

#ifdef USE_ESP32

#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

#include <algorithm>
#include <cinttypes>
#include <cmath>

namespace esphome::aec_tuning {

static const char *const TAG = "aec_tuning";

namespace xvf = aivi_xvf3800;
using aec_self_test::Action;
using aec_self_test::Engine;
using aec_self_test::Error;
using aec_self_test::Step;

// 2 s of 16 kHz stereo 16-bit audio between the microphone task and loop().
static constexpr size_t RING_BUFFER_BYTES = 2 * 16000 * 2 * 2;
static constexpr size_t MAX_INPUT_BYTES_PER_LOOP = 16384;
static constexpr uint32_t TUNING_RETRY_MS = 1000;
static constexpr uint32_t CONVERGED_POLL_MS = 2000;
// The whole test takes about 35 s; the engine has its own sample-count
// timeouts, this one catches a stalled microphone.
static constexpr uint32_t SELF_TEST_TIMEOUT_MS = 120000;
static constexpr float REF_GAIN_MIN = 0.1f;
static constexpr float REF_GAIN_MAX = 8.0f;

void AecTuning::setup() {
  this->pref_ = global_preferences->make_preference<SavedTuning>(fnv1_hash("aec_tuning"), true);
  if (!this->pref_.load(&this->saved_))
    this->saved_ = SavedTuning{0, 0.0f, 0};

  // Kept for good: the microphone task may still be writing when a test ends.
  this->ring_buffer_ = ring_buffer::RingBuffer::create(RING_BUFFER_BYTES);
  if (this->ring_buffer_ == nullptr)
    ESP_LOGE(TAG, "Could not allocate the %u byte audio buffer; no self test", static_cast<unsigned>(RING_BUFFER_BYTES));

  this->microphone_source_->add_data_callback([this](const std::vector<uint8_t> &data) {
    // Runs in the microphone task: copy only, never block.
    if (!this->capturing_.load(std::memory_order_acquire))
      return;
    const size_t written = this->ring_buffer_->write_without_replacement(data.data(), data.size(), 0, false);
    if (written < data.size())
      this->dropped_bytes_.fetch_add(data.size() - written, std::memory_order_relaxed);
  });

#ifdef USE_OTA_STATE_LISTENER
  ota::get_global_ota_callback()->add_global_state_listener(this);
#endif
}

void AecTuning::dump_config() {
  ESP_LOGCONFIG(TAG, "AEC tuning:");
  if (this->saved_.flags & kSavedSysDelay)
    ESP_LOGCONFIG(TAG, "  Saved SYS_DELAY: %" PRId32, this->saved_.sys_delay);
  if (this->saved_.flags & kSavedRefGain)
    ESP_LOGCONFIG(TAG, "  Saved REF_GAIN: %.3f", this->saved_.ref_gain);
  if (this->saved_.flags == 0)
    ESP_LOGCONFIG(TAG, "  No saved settings: XVF3800 defaults");
}

void AecTuning::loop() {
  if (this->respeaker_->is_failed())
    return;
  if (this->testing_) {
    this->run_self_test_();
    return;
  }
  const uint32_t now = millis();
  if (!this->tuning_ready_) {
    if (static_cast<int32_t>(now - this->next_tuning_try_ms_) >= 0) {
      this->next_tuning_try_ms_ = now + TUNING_RETRY_MS;
      this->boot_tuning_();
    }
    return;
  }
  if (now - this->last_poll_ms_ >= CONVERGED_POLL_MS) {
    this->last_poll_ms_ = now;
    this->poll_converged_();
  }
}

// --- Settings ---

void AecTuning::boot_tuning_() {
  // The audio servicers answer once the XVF3800's pipeline runs, a moment
  // after its control interface.
  int32_t sys_delay;
  float ref_gain;
  if (!xvf::read_int32(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrSysDelay, sys_delay) ||
      !xvf::read_float(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrRefGain, ref_gain))
    return;
  if (this->saved_.flags & kSavedSysDelay) {
    if (xvf::write_int32(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrSysDelay, this->saved_.sys_delay)) {
      sys_delay = this->saved_.sys_delay;
    } else {
      ESP_LOGW(TAG, "Could not write the saved SYS_DELAY");
    }
  }
  if (this->saved_.flags & kSavedRefGain) {
    if (xvf::write_float(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrRefGain, this->saved_.ref_gain)) {
      ref_gain = this->saved_.ref_gain;
    } else {
      ESP_LOGW(TAG, "Could not write the saved REF_GAIN");
    }
  }
  this->sys_delay_ = static_cast<float>(sys_delay);
  this->ref_gain_ = ref_gain;
  this->tuning_ready_ = true;
  ESP_LOGI(TAG, "SYS_DELAY %" PRId32 ", REF_GAIN %.3f%s", sys_delay, ref_gain,
           this->saved_.flags ? " (saved settings)" : " (XVF3800 defaults)");
}

bool AecTuning::set_sys_delay(float value) {
  if (this->testing_ || !std::isfinite(value))
    return false;
  const int32_t samples = static_cast<int32_t>(
      std::lround(std::max<float>(aec_self_test::kSysDelayMin, std::min<float>(aec_self_test::kSysDelayMax, value))));
  if (!xvf::write_int32(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrSysDelay, samples)) {
    ESP_LOGW(TAG, "Could not write SYS_DELAY %" PRId32, samples);
    return false;
  }
  this->sys_delay_ = static_cast<float>(samples);
  this->saved_.sys_delay = samples;
  this->saved_.flags |= kSavedSysDelay;
  this->save_tuning_();
  ESP_LOGI(TAG, "SYS_DELAY %" PRId32, samples);
  return true;
}

bool AecTuning::set_ref_gain(float value) {
  if (this->testing_ || !std::isfinite(value))
    return false;
  const float gain = std::max(REF_GAIN_MIN, std::min(REF_GAIN_MAX, value));
  if (!xvf::write_float(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrRefGain, gain)) {
    ESP_LOGW(TAG, "Could not write REF_GAIN %.3f", gain);
    return false;
  }
  this->ref_gain_ = gain;
  this->saved_.ref_gain = gain;
  this->saved_.flags |= kSavedRefGain;
  this->save_tuning_();
  ESP_LOGI(TAG, "REF_GAIN %.3f", gain);
  return true;
}

void AecTuning::save_tuning_() {
  if (!this->pref_.save(&this->saved_))
    ESP_LOGW(TAG, "Could not save the settings");
}

void AecTuning::poll_converged_() {
  int32_t value;
  if (xvf::read_int32(this->respeaker_, xvf::kAecResid, xvf::kAecConverged, value))
    this->converged_ = value != 0 ? 1 : 0;
}

// --- Self test ---

bool AecTuning::start_self_test() {
  if (this->testing_) {
    ESP_LOGW(TAG, "A self test is already running");
    return false;
  }
  if (this->respeaker_->is_failed() || !this->tuning_ready_) {
    this->report_("error:xvf_io");
    return false;
  }
  RAMAllocator<int16_t> allocator(RAMAllocator<int16_t>::ALLOC_EXTERNAL);
  this->engine_buffer_ = this->ring_buffer_ != nullptr ? allocator.allocate(aec_self_test::kBufferSamples) : nullptr;
  if (this->engine_buffer_ == nullptr) {
    ESP_LOGE(TAG, "Not enough PSRAM for the self test");
    this->report_("error:memory");
    return false;
  }
  this->ring_buffer_->reset();

  this->engine_.set_buffer(this->engine_buffer_);
  this->engine_.start();
  this->before_valid_ = false;
  this->out_bytes_ = this->out_offset_ = 0;
  this->dropped_bytes_.store(0, std::memory_order_relaxed);
  this->reported_step_ = Step::IDLE;
  this->test_started_ms_ = millis();
  this->testing_ = true;

  this->speaker_->set_audio_stream_info(audio::AudioStreamInfo(16, 1, aec_self_test::kOutRate));
  this->speaker_->start();
  this->capturing_.store(true, std::memory_order_release);
  this->microphone_source_->start();
  ESP_LOGI(TAG, "Self test started");
  return true;
}

void AecTuning::abort_self_test() {
  if (!this->testing_)
    return;
  this->engine_.abort();
}

#ifdef USE_OTA_STATE_LISTENER
void AecTuning::on_ota_global_state(ota::OTAState state, float progress, uint8_t error, ota::OTAComponent *comp) {
  // The upload blocks the loop: restore the XVF3800 now.
  if (state == ota::OTA_STARTED && this->testing_) {
    this->engine_.abort();
    this->run_action_();
    this->finish_self_test_();
  }
}
#endif

void AecTuning::run_self_test_() {
  if (this->engine_.action() != Action::NONE)
    this->run_action_();
  if (this->engine_.running()) {
    this->feed_speaker_();
    this->read_input_();
    this->engine_.work();
    if (millis() - this->test_started_ms_ > SELF_TEST_TIMEOUT_MS)
      this->engine_.fail(Error::TIMEOUT);
  }
  // A failure asks for a restore before the engine stops.
  if (this->engine_.action() != Action::NONE)
    this->run_action_();

  const Step step = this->engine_.step();
  if (step != this->reported_step_ && this->engine_.running()) {
    this->reported_step_ = step;
    this->report_(std::string("running:") + Engine::step_name(step));
  }
  if (!this->engine_.running())
    this->finish_self_test_();
}

void AecTuning::run_action_() {
  const Action action = this->engine_.action();
  const aec_self_test::Results &res = this->engine_.results();
  bool ok = true;
  switch (action) {
    case Action::SETUP_A: {
      int32_t sys_delay;
      float ref_gain;
      ok = xvf::read_mux(this->respeaker_, xvf::kAudioMgrOpLeft, this->mux_left_before_) &&
           xvf::read_mux(this->respeaker_, xvf::kAudioMgrOpRight, this->mux_right_before_) &&
           xvf::read_int32(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrSysDelay, sys_delay) &&
           xvf::read_float(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrRefGain, ref_gain);
      if (ok) {
        this->before_valid_ = true;
        this->sys_delay_before_ = sys_delay;
        this->ref_gain_before_ = ref_gain;
        this->engine_.set_current_tuning(sys_delay, ref_gain);
        ok = xvf::write_int32(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrSysDelay, 0) &&
             xvf::write_float(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrRefGain,
                              aec_self_test::kMeasureRefGain) &&
             xvf::write_mux(this->respeaker_, xvf::kAudioMgrOpLeft, {xvf::kMuxMicPostGain, 0}) &&
             xvf::write_mux(this->respeaker_, xvf::kAudioMgrOpRight, {xvf::kMuxRefPostGain, 0});
      }
      ESP_LOGI(TAG, "Before: SYS_DELAY %" PRId32 ", REF_GAIN %.3f, mux L %u/%u R %u/%u", this->sys_delay_before_,
               this->ref_gain_before_, this->mux_left_before_.category, this->mux_left_before_.source,
               this->mux_right_before_.category, this->mux_right_before_.source);
      break;
    }
    case Action::SETUP_B:
      // ref_gain_new is the gain before the test unless the test changed it.
      ok = xvf::write_int32(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrSysDelay, res.sys_delay_new) &&
           xvf::write_float(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrRefGain, res.ref_gain_new) &&
           xvf::write_mux(this->respeaker_, xvf::kAudioMgrOpRight, {xvf::kMuxAecResidual, 0});
      break;
    case Action::READ_CONVERGED: {
      int32_t value;
      ok = xvf::read_int32(this->respeaker_, xvf::kAecResid, xvf::kAecConverged, value);
      if (ok) {
        this->engine_.set_converged(value != 0);
        this->converged_ = value != 0 ? 1 : 0;
      }
      break;
    }
    case Action::RESTORE_MUX:
      ok = xvf::write_mux(this->respeaker_, xvf::kAudioMgrOpLeft, this->mux_left_before_) &&
           xvf::write_mux(this->respeaker_, xvf::kAudioMgrOpRight, this->mux_right_before_);
      break;
    case Action::RESTORE_ALL:
      // Only what was read before the test; the engine stops either way.
      if (this->before_valid_) {
        const bool restored =
            xvf::write_mux(this->respeaker_, xvf::kAudioMgrOpLeft, this->mux_left_before_) &&
            xvf::write_mux(this->respeaker_, xvf::kAudioMgrOpRight, this->mux_right_before_) &&
            xvf::write_int32(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrSysDelay, this->sys_delay_before_) &&
            xvf::write_float(this->respeaker_, xvf::kAudioMgrResid, xvf::kAudioMgrRefGain, this->ref_gain_before_);
        if (!restored)
          ESP_LOGE(TAG, "Could not restore the XVF3800 settings; restart the satellite");
      }
      break;
    default:
      return;
  }
  if (!ok)
    ESP_LOGW(TAG, "XVF3800 control failed during step %s", Engine::step_name(this->engine_.step()));
  // Audio from before the change is useless; the engine also skips the
  // first 300 ms after a mux change.
  this->ring_buffer_->reset();
  this->engine_.action_done(ok);
}

void AecTuning::feed_speaker_() {
  for (;;) {
    if (this->out_offset_ >= this->out_bytes_) {
      const size_t samples = this->engine_.read_output(this->out_, kOutSamples);
      this->out_bytes_ = samples * sizeof(int16_t);
      this->out_offset_ = 0;
      if (samples == 0)
        return;
    }
    const size_t left = this->out_bytes_ - this->out_offset_;
    const size_t written =
        this->speaker_->play(reinterpret_cast<const uint8_t *>(this->out_) + this->out_offset_, left, 0);
    this->out_offset_ += written;
    if (written < left)
      return;  // the speaker's buffer is full
  }
}

void AecTuning::read_input_() {
  size_t budget = MAX_INPUT_BYTES_PER_LOOP;
  while (budget > 0) {
    size_t bytes = std::min(this->ring_buffer_->available(), std::min(budget, sizeof(this->in_)));
    bytes -= bytes % (2 * sizeof(int16_t));
    if (bytes == 0)
      return;
    const size_t got = this->ring_buffer_->read(this->in_, bytes, 0);
    this->engine_.write_input(this->in_, got / (2 * sizeof(int16_t)));
    budget -= got;
    if (got < bytes)
      return;
  }
}

void AecTuning::finish_self_test_() {
  this->capturing_.store(false, std::memory_order_release);
  this->microphone_source_->stop();
  this->speaker_->stop();
  this->testing_ = false;

  const aec_self_test::Results &res = this->engine_.results();
  const uint32_t dropped = this->dropped_bytes_.load(std::memory_order_relaxed);
  if (dropped > 0)
    ESP_LOGW(TAG, "%" PRIu32 " bytes of test audio dropped", dropped);

  if (this->engine_.step() == Step::DONE) {
    // The test wrote the new values; keep them for the next boot.
    this->sys_delay_ = static_cast<float>(res.sys_delay_new);
    this->saved_.sys_delay = res.sys_delay_new;
    this->saved_.flags |= kSavedSysDelay;
    if (std::isfinite(res.ref_gain_new)) {
      this->ref_gain_ = res.ref_gain_new;
      this->saved_.ref_gain = res.ref_gain_new;
      this->saved_.flags |= kSavedRefGain;
    }
    this->save_tuning_();
    ESP_LOGI(TAG, "Self test done: delay %d samples (spread %d), SYS_DELAY %d -> %d, REF_GAIN %.3f -> %.3f",
             res.delay_samples, res.delay_spread, res.sys_delay_old, res.sys_delay_new, res.ref_gain_old,
             res.ref_gain_new);
    ESP_LOGI(TAG, "  echo to reference %.1f dB, residual delay %.1f ms, usable tail %.0f ms", res.echo_to_ref_db,
             res.residual_ms, res.usable_tail_ms);
    ESP_LOGI(TAG, "  RT60 %.0f ms, tail coverage %.1f dB, ERLE %.1f dB (bands %.1f), converged %d, warnings 0x%02" PRIx32,
             res.rt60_ms, res.tail_coverage_db, res.erle_db, res.erle_band_db, res.converged ? 1 : 0, res.warnings);
    for (size_t k = 0; k < aec_self_test::kBands; k++) {
      const aec_self_test::BandResult &band = res.bands[k];
      ESP_LOGI(TAG, "  %4.0f Hz: echo %.1f dB, SNR %.1f dB, RT60 %.0f ms (T%u), ERLE %.1f dB",
               aec_self_test::kBandHz[k], band.echo_gain_db, band.snr_db, band.rt60_ms, band.rt_range_db,
               band.erle_db);
    }
  } else {
    if (this->before_valid_) {
      this->sys_delay_ = static_cast<float>(this->sys_delay_before_);
      this->ref_gain_ = this->ref_gain_before_;
    }
    ESP_LOGW(TAG, "Self test failed: %s", this->error_code());
  }

  RAMAllocator<int16_t> allocator(RAMAllocator<int16_t>::ALLOC_EXTERNAL);
  this->engine_.set_buffer(nullptr);
  allocator.deallocate(this->engine_buffer_, aec_self_test::kBufferSamples);
  this->engine_buffer_ = nullptr;

  this->report_(this->engine_.step() == Step::DONE ? "done" : std::string("error:") + this->error_code());
}

void AecTuning::report_(const std::string &status) {
  this->defer([this, status]() { this->status_trigger_.trigger(status); });
}

}  // namespace esphome::aec_tuning

#endif  // USE_ESP32
