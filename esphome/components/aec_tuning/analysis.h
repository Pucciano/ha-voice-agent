// AIVI satellite: echo canceller self test engine (issue #17)
//
// Plain C++17 without ESPHome, so tests/aec_tuning can run it on the host.
// The aec_tuning component feeds it and carries out its XVF3800 actions.
//
// The engine plays test signals through the jack (48 kHz mono, through the
// mixer) and listens to two XVF3800 output channels (16 kHz stereo):
//
//   phase A  L = microphone 0 after gain, R = reference after gain, with
//            AUDIO_MGR_SYS_DELAY at 0 and AUDIO_MGR_REF_GAIN at 1 (formatBCE's
//            firmware ships 8, which would clip the test signal)
//     floor    1 s silence: noise floor per band
//     pilot    1 kHz until the microphone hears it, up to 20 s (the room
//              speaker may still be switching to its analog input, or waking
//              its amplifier from standby)
//     bursts   3 x 0.5 s noise: echo delay by cross-correlation, reference
//              and echo levels
//     tones    125 Hz to 4 kHz, 1.2 s each, then 1.0 s silence: echo path
//              gain, signal to noise ratio and reverberation time per band.
//              Warble tones (+-10 % at 5 Hz), so room modes do not beat in
//              the decay.
//   apply    AUDIO_MGR_SYS_DELAY and AUDIO_MGR_REF_GAIN from phase A
//   phase B  L = microphone 0 after gain, R = echo canceller residual
//     noise    20 s: echo attenuation (ERLE) after 5, 10, 15 and 20 s, and
//              per band over the last 5 s. The settings of phase A moved
//              the echo in the filter, so the canceller adapts anew.
//
// The attenuation test (Mode::ATTENUATION) changes no setting: floor, the
// pilot until the echo is heard, then phase B with the current settings.
//
// Every phase A segment is found by its onset in the reference channel, so
// buffering in the output path never shifts a measurement window.
#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <new>
#include <vector>

namespace aec_self_test {

constexpr uint32_t kInRate = 16000;   // XVF3800 output as the ESP32 receives it
constexpr uint32_t kOutRate = 48000;  // mixer rate
constexpr uint32_t kBlock = 160;      // 10 ms analysis block
constexpr float kBlockMs = 10.0f;

constexpr size_t kBands = 6;
constexpr float kBandHz[kBands] = {125.0f, 250.0f, 500.0f, 1000.0f, 2000.0f, 4000.0f};
constexpr size_t kPilotBand = 3;  // 1 kHz
constexpr size_t kBursts = 3;

// XMOS XVF3800 v3.2.1: the echo filter covers 192 ms; AUDIO_MGR_SYS_DELAY
// delays the reference by -64..256 samples, and the echo should trail the
// reference by at most 40 samples. 24 keep the filter causal with margin.
constexpr float kAecTailMs = 192.0f;
constexpr int kSysDelayMin = -64;
constexpr int kSysDelayMax = 256;
constexpr int kDelayMargin = 24;
constexpr int kMaxSpread = 16;  // delay spread over the bursts, samples
// XMOS: the post-gain reference as high as possible, with 1-2 dB headroom.
constexpr float kRefTargetDbfs = -2.0f;
constexpr float kMeasureRefGain = 1.0f;  // during phase A
constexpr float kRefGainMin = 0.25f;
constexpr float kRefGainMax = 8.0f;
constexpr float kRefGainStepDb = 1.0f;  // smaller changes are not applied
// XMOS: the microphone should peak at least 6 dB below the reference.
constexpr float kEchoToRefMaxDb = -6.0f;

constexpr float kWarbleDepth = 0.1f;
constexpr float kWarbleHz = 5.0f;

// Test signal levels, dBFS of a full-scale sine peak or RMS of the noise.
constexpr float kPilotPeak = 0.1f;     // -20 dBFS
constexpr float kTonePeak = 0.5f;      // -6 dBFS
constexpr float kNoiseRms = 0.2512f;   // -12 dBFS; peaks stay below -1.2 dBFS

// Durations at the output rate.
constexpr uint32_t ms_out(uint32_t ms) { return ms * (kOutRate / 1000); }
constexpr uint32_t ms_in(uint32_t ms) { return ms * (kInRate / 1000); }
constexpr uint32_t kBurstMs = 500;
constexpr uint32_t kBurstGapMs = 700;
constexpr uint32_t kToneMs = 1200;
constexpr uint32_t kDecayMs = 1000;
constexpr uint32_t kConvergeMs = 20000;
// ERLE windows in phase B, in 10 ms blocks from the noise onset: the first
// skips the onset, the last ends before the noise.
constexpr size_t kErleWindows = 4;
constexpr uint32_t kErleWindowStart[kErleWindows] = {50, 500, 1000, 1500};
constexpr uint32_t kErleWindowEnd[kErleWindows] = {500, 1000, 1500, 1950};
constexpr float kErleWindowSeconds[kErleWindows] = {5.0f, 10.0f, 15.0f, 20.0f};

// Correlation window: 4096 reference samples against lags -64..3199
// (-4..200 ms).
constexpr int kNegLag = 64;
constexpr int kLags = 3264;
constexpr size_t kCorrN = 4096;
constexpr size_t kCorrL = kCorrN + kLags;
constexpr float kMinCoeff = 0.1f;   // weaker peaks are not an echo
// Audio memory for the bursts, provided by the caller (PSRAM on the device).
constexpr size_t kBufferSamples = kBursts * (kCorrN + kCorrL);
constexpr int kLagsPerWork = 48;    // about 0.2 M multiply-adds per work()

// Detection thresholds.
constexpr float kRefActiveDbfs = -50.0f;  // block RMS
constexpr float kRefQuietDbfs = -60.0f;   // block RMS
constexpr uint32_t kQuietBlocks = 20;     // 200 ms of quiet reference arms an onset
constexpr float kPilotEchoDb = 15.0f;     // pilot band over the floor
constexpr uint32_t kPilotEchoBlocks = 20;
constexpr float kOnsetBDb = 10.0f;        // phase B microphone over the floor, broadband

// Sample-count timeouts (the input is the clock).
constexpr uint32_t kSettleMs = 300;
constexpr uint32_t kFloorMs = 1000;
constexpr uint32_t kNoReferenceMs = 3000;
// The Pulse Flex's amplifier needs 5-15 s to wake from standby (Bluesound
// support); the pilot waits for it, and switch_ms reports the time.
constexpr uint32_t kNoEchoMs = 20000;
constexpr uint32_t kSegmentTimeoutMs = 4000;
constexpr uint32_t kOnsetBTimeoutMs = 3000;

// Reverberation fit on the Schroeder curve.
constexpr float kFitStartDb = -5.0f;
constexpr uint32_t kDecayBlocks = 120;  // stored per tone, from the reference's end
constexpr uint32_t kDecayUsable = 97;   // blocks used, ending before the next tone's echo

// Steady tone level in dBFS (RMS of a sine, relative to a full-scale square).
inline float tone_dbfs(float peak) { return 20.0f * std::log10(peak) - 3.0103f; }

inline float to_db(double energy) { return energy > 0.0 ? 10.0f * static_cast<float>(std::log10(energy)) : -200.0f; }
// dBFS of a mean square, relative to a full-scale square wave.
inline float ms_dbfs(double mean_square) { return to_db(mean_square / (32768.0 * 32768.0)); }

enum class Step : uint8_t {
  IDLE,
  SETUP_A,
  FLOOR,
  PILOT,
  MEASURE_A,
  SETUP_B,
  MEASURE_B,
  READ_CONVERGED,
  RESTORE,
  DONE,
  FAILED,
};

// XVF3800 work the caller carries out, then confirms with action_done().
enum class Action : uint8_t {
  NONE,
  SETUP_A,         // read SYS_DELAY and REF_GAIN (set_current_tuning), save
                   // the output mux, then SYS_DELAY 0, REF_GAIN
                   // kMeasureRefGain, L (3,0), R (5,0)
  SETUP_B,         // SYS_DELAY and REF_GAIN from the results, R (7,0)
  SETUP_ATTENUATION,  // read SYS_DELAY and REF_GAIN (set_current_tuning),
                      // save the output mux, then L (3,0), R (7,0)
  READ_CONVERGED,  // AEC_AECCONVERGED (set_converged)
  RESTORE_MUX,     // the saved output mux
  RESTORE_ALL,     // the saved output mux, SYS_DELAY and REF_GAIN
};

enum class Error : uint8_t {
  NONE,
  NO_REFERENCE,  // the reference channel stays silent: playback does not reach the XVF3800
  NO_ECHO,       // the microphones do not hear the test signal
  WEAK_ECHO,     // no clear correlation peak in at least two bursts
  INVALID_MUX,   // the residual is louder than the microphone
  XVF_IO,        // a control read or write failed
  TIMEOUT,       // a segment never arrived
  ABORTED,
};

enum Warning : uint32_t {
  WARN_DELAY_UNSTABLE = 1 << 0,  // spread over kMaxSpread: SYS_DELAY not changed
  WARN_DELAY_CLAMPED = 1 << 1,   // more delay than SYS_DELAY can take
  WARN_CLIPPING = 1 << 2,        // the microphone signal clips
  WARN_ECHO_LOUD = 1 << 3,       // echo less than 6 dB below the reference
  WARN_REVERB_LONG = 1 << 4,     // the echo filter covers less than 20 dB of the decay
  WARN_LOW_SNR = 1 << 5,         // a band too quiet for a reverberation time
  WARN_NOT_CONVERGED = 1 << 6,
  WARN_LOW_ERLE = 1 << 7,        // under 10 dB
  WARN_REF_CLIPPED = 1 << 8,     // the reference clipped: REF_GAIN not changed
};

struct BandResult {
  float ref_dbfs{NAN};      // reference after gain, steady tone
  float echo_gain_db{NAN};  // microphone over reference, steady tone
  float snr_db{NAN};        // steady tone over the noise floor
  float rt60_ms{NAN};
  uint8_t rt_range_db{0};   // 20 (T20), 10 (T10) or 0 (not measurable)
  float erle_db{NAN};
};

struct Results {
  float floor_dbfs{NAN};     // microphone, broadband
  float echo_wait_ms{NAN};   // first reference to audible pilot echo
  float switch_ms{NAN};      // how much later than the pilot the room speaker played
  std::array<int, kBursts> burst_delay{};
  std::array<float, kBursts> burst_coeff{};
  int valid_bursts{0};
  int delay_samples{0};
  int delay_spread{0};
  float delay_ms{NAN};
  float ref_rms_dbfs{NAN};
  float ref_peak_dbfs{NAN};
  float mic_rms_dbfs{NAN};
  float mic_peak_dbfs{NAN};
  float echo_to_ref_db{NAN};  // peaks, with the new reference gain
  bool clipped{false};
  bool ref_clipped{false};
  int sys_delay_old{0};
  int sys_delay_new{0};
  float ref_gain_old{NAN};
  float ref_gain_new{NAN};
  float residual_ms{NAN};     // echo delay the filter still has to cover
  float usable_tail_ms{NAN};
  std::array<BandResult, kBands> bands{};
  float rt60_ms{NAN};         // median of 500 Hz to 2 kHz
  float tail_coverage_db{NAN};
  bool attenuation_only{false};
  float erle_db{NAN};         // broadband, last window
  std::array<float, kErleWindows> erle_trend{NAN, NAN, NAN, NAN};  // broadband per window
  float erle_band_db{NAN};    // median over the bands
  bool converged_read{false};
  bool converged{false};
  uint32_t warnings{0};
};

/// Biquad band pass (RBJ, 0 dB peak gain).
class BandPass {
 public:
  void setup(float hz, float q, float rate) {
    const float w = 2.0f * static_cast<float>(M_PI) * hz / rate;
    const float alpha = std::sin(w) / (2.0f * q);
    const float a0 = 1.0f + alpha;
    b0_ = alpha / a0;
    b2_ = -alpha / a0;
    a1_ = -2.0f * std::cos(w) / a0;
    a2_ = (1.0f - alpha) / a0;
    reset();
  }
  void reset() { x1_ = x2_ = y1_ = y2_ = 0.0f; }
  float process(float x) {
    const float y = b0_ * x + b2_ * x2_ - a1_ * y1_ - a2_ * y2_;
    x2_ = x1_;
    x1_ = x;
    y2_ = y1_;
    y1_ = y;
    return y;
  }

 private:
  float b0_{0}, b2_{0}, a1_{0}, a2_{0};
  float x1_{0}, x2_{0}, y1_{0}, y2_{0};
};

/// Test signal source. Plays the pilot until stop_pilot(), then the loaded
/// program; segments follow each other without gaps.
class Generator {
 public:
  enum class Kind : uint8_t { SILENCE, NOISE, TONE };
  struct Segment {
    Kind kind;
    float hz;
    uint32_t samples;
  };

  void reset(uint32_t seed) {
    seed_ = seed ? seed : 1;
    program_.clear();
    index_ = 0;
    position_ = 0;
    pilot_ = false;
    pilot_stopping_ = false;
    pilot_position_ = 0;
    phase_ = 0.0f;
  }
  void load(std::vector<Segment> program) {
    program_ = std::move(program);
    index_ = 0;
    position_ = 0;
    phase_ = 0.0f;
  }
  void start_pilot() {
    pilot_ = true;
    pilot_stopping_ = false;
    pilot_position_ = 0;
    phase_ = 0.0f;
  }
  void stop_pilot() {
    if (pilot_ && !pilot_stopping_) {
      pilot_stopping_ = true;
      pilot_ramp_ = 0;
    }
  }
  bool playing() const { return pilot_ || index_ < program_.size(); }

  /// Up to n samples; fewer once the program has ended.
  size_t read(int16_t *out, size_t n) {
    size_t written = 0;
    while (written < n) {
      if (pilot_) {
        out[written++] = to_pcm(pilot_sample_());
        continue;
      }
      if (index_ >= program_.size())
        break;
      const Segment &segment = program_[index_];
      out[written++] = to_pcm(segment_sample_(segment, position_));
      if (++position_ >= segment.samples) {
        index_++;
        position_ = 0;
        phase_ = 0.0f;
      }
    }
    return written;
  }

  /// Uniform in [-1, 1), xorshift32.
  float uniform() {
    seed_ ^= seed_ << 13;
    seed_ ^= seed_ >> 17;
    seed_ ^= seed_ << 5;
    return static_cast<float>(seed_) / 2147483648.0f - 1.0f;
  }
  /// Gaussian approximation with unit variance, bounded to +-3.46.
  float gaussian() {
    // Sum of four uniforms (Irwin-Hall) has variance 4/3.
    return (uniform() + uniform() + uniform() + uniform()) * 0.8660254f;
  }

  static int16_t to_pcm(float x) {
    const float scaled = std::round(x * 32767.0f);
    return static_cast<int16_t>(std::max(-32768.0f, std::min(32767.0f, scaled)));
  }

 protected:
  static constexpr uint32_t kToneRamp = ms_out(10);
  static constexpr uint32_t kNoiseRamp = ms_out(5);

  static float ramp(uint32_t position, uint32_t samples, uint32_t length) {
    if (position < length)
      return 0.5f - 0.5f * std::cos(static_cast<float>(M_PI) * position / length);
    if (position + length > samples)
      return 0.5f - 0.5f * std::cos(static_cast<float>(M_PI) * (samples - position) / length);
    return 1.0f;
  }

  // Single precision: the ESP32-S3 has no double precision FPU.
  static constexpr float kTwoPi = 6.28318531f;

  float sine_(float hz) {
    const float value = std::sin(phase_);
    phase_ += kTwoPi * hz / kOutRate;
    if (phase_ > kTwoPi)
      phase_ -= kTwoPi;
    return value;
  }

  float warble_(float hz, uint32_t position) {
    // The modulation phase, wrapped per period so it stays exact.
    constexpr uint32_t kPeriod = static_cast<uint32_t>(kOutRate / kWarbleHz);
    const float t = static_cast<float>(position % kPeriod) / kPeriod;
    return sine_(hz * (1.0f + kWarbleDepth * std::sin(kTwoPi * t)));
  }

  float pilot_sample_() {
    float gain = 1.0f;
    if (pilot_position_ < kToneRamp) {
      gain = 0.5f - 0.5f * std::cos(static_cast<float>(M_PI) * pilot_position_ / kToneRamp);
      pilot_position_++;
    }
    if (pilot_stopping_) {
      gain *= 0.5f + 0.5f * std::cos(static_cast<float>(M_PI) * pilot_ramp_ / kToneRamp);
      if (++pilot_ramp_ >= kToneRamp) {
        pilot_ = false;
        phase_ = 0.0f;
      }
    }
    return kPilotPeak * gain * sine_(kBandHz[kPilotBand]);
  }

  float segment_sample_(const Segment &segment, uint32_t position) {
    switch (segment.kind) {
      case Kind::NOISE:
        return kNoiseRms * gaussian() * ramp(position, segment.samples, kNoiseRamp);
      case Kind::TONE:
        return kTonePeak * warble_(segment.hz, position) * ramp(position, segment.samples, kToneRamp);
      default:
        return 0.0f;
    }
  }

  uint32_t seed_{1};
  std::vector<Segment> program_;
  size_t index_{0};
  uint32_t position_{0};
  bool pilot_{false};
  bool pilot_stopping_{false};
  uint32_t pilot_position_{0};
  uint32_t pilot_ramp_{0};
  float phase_{0.0f};
};

/// Reverberation time from block energies of a decay, noise floor removed:
/// Schroeder backward integration, then a least squares line from -5 dB to
/// -25 dB (T20) or, with less dynamic range, to -15 dB (T10). The decay must
/// start 10 dB above the end of the fit range, so the fit stays clear of
/// the noise floor and of the end of the integration.
inline float reverberation_ms(const float *energy, size_t count, float floor, uint8_t &range_db) {
  range_db = 0;
  if (count < 8)
    return NAN;
  const double start = (static_cast<double>(energy[0]) + energy[1] + energy[2]) / 3.0;
  const float dynamic_db = floor > 0.0f ? to_db(start / floor) : 200.0f;
  // Integrate up to where the decay, averaged over 50 ms, meets the floor
  // (3 dB above it). The floor is subtracted without clipping at zero, so
  // the noise left in the tail averages out instead of lengthening it.
  size_t end = count;
  for (size_t i = 0; i + 5 <= count; i++) {
    const double average = (static_cast<double>(energy[i]) + energy[i + 1] + energy[i + 2] + energy[i + 3] +
                            energy[i + 4]) / 5.0;
    if (average < 2.0 * floor) {
      end = i + 2;
      break;
    }
  }
  if (end < 5)
    return NAN;
  std::vector<double> curve(end);
  double sum = 0.0;
  for (size_t i = end; i-- > 0;) {
    sum += static_cast<double>(energy[i]) - floor;
    curve[i] = std::max(sum, 1e-12);
  }
  if (curve[0] <= 1e-12)
    return NAN;
  std::vector<float> db(end);
  for (size_t i = 0; i < end; i++)
    db[i] = to_db(curve[i] / curve[0]);

  for (uint8_t range : {uint8_t(20), uint8_t(10)}) {
    const float lower = kFitStartDb - range;
    if (dynamic_db < -lower + 10.0f || db[end - 1] > lower)
      continue;
    double n = 0, sx = 0, sy = 0, sxx = 0, sxy = 0;
    for (size_t i = 0; i < end; i++) {
      if (db[i] > kFitStartDb || db[i] < lower)
        continue;
      n++;
      sx += i;
      sy += db[i];
      sxx += static_cast<double>(i) * i;
      sxy += i * static_cast<double>(db[i]);
    }
    if (n < 4)
      continue;
    const double slope = (n * sxy - sx * sy) / (n * sxx - sx * sx);  // dB per block
    if (slope >= 0.0)
      continue;
    range_db = range;
    return static_cast<float>(-60.0 / slope * kBlockMs);
  }
  return NAN;
}

inline float median(std::vector<float> values) {
  values.erase(std::remove_if(values.begin(), values.end(), [](float v) { return std::isnan(v); }), values.end());
  if (values.empty())
    return NAN;
  std::sort(values.begin(), values.end());
  const size_t mid = values.size() / 2;
  return values.size() % 2 ? values[mid] : 0.5f * (values[mid - 1] + values[mid]);
}

class Engine {
 public:
  /// kBufferSamples of audio memory; it must outlive the engine.
  void set_buffer(int16_t *buffer) { buffer_ = buffer; }

  enum class Mode : uint8_t { FULL, ATTENUATION };

  /// Starts a test. Without a buffer the engine fails at once.
  void start(Mode mode = Mode::FULL, uint32_t seed = 0x41495649) {
    // Rebuilt in place: a temporary would not fit on the ESP32's loop stack.
    int16_t *buffer = buffer_;
    this->~Engine();
    new (this) Engine();
    buffer_ = buffer;
    mode_ = mode;
    results_.attenuation_only = mode == Mode::ATTENUATION;
    seed_ = seed;
    generator_.reset(seed);
    for (size_t k = 0; k < kBands; k++) {
      // About one octave wide.
      left_bands_[k].setup(kBandHz[k], 1.41f, kInRate);
      right_bands_[k].setup(kBandHz[k], 1.41f, kInRate);
    }
    set_step_(Step::SETUP_A);
    if (buffer_ == nullptr) {
      error_ = Error::XVF_IO;
      set_step_(Step::FAILED);
      return;
    }
    std::memset(buffer_, 0, kBufferSamples * sizeof(int16_t));
    for (size_t b = 0; b < kBursts; b++) {
      bursts_[b].r = buffer_ + b * (kCorrN + kCorrL);
      bursts_[b].l = bursts_[b].r + kCorrN;
    }
    action_ = mode == Mode::FULL ? Action::SETUP_A : Action::SETUP_ATTENUATION;
  }

  /// Stops a running test; the saved settings come back.
  void abort() { fail_(Error::ABORTED); }
  void fail(Error error) { fail_(error); }

  Step step() const { return step_; }
  bool running() const { return step_ != Step::IDLE && step_ != Step::DONE && step_ != Step::FAILED; }
  Error error() const { return error_; }
  const Results &results() const { return results_; }
  Action action() const { return action_; }
  /// Input processed so far, in ms.
  uint32_t input_ms() const { return static_cast<uint32_t>(input_frames_ / (kInRate / 1000)); }

  static const char *step_name(Step step) {
    switch (step) {
      case Step::SETUP_A:
        return "setup";
      case Step::FLOOR:
        return "noise_floor";
      case Step::PILOT:
        return "pilot";
      case Step::MEASURE_A:
        return "delay_and_tones";
      case Step::SETUP_B:
        return "apply";
      case Step::MEASURE_B:
        return "attenuation";
      case Step::READ_CONVERGED:
      case Step::RESTORE:
        return "finish";
      case Step::DONE:
        return "done";
      case Step::FAILED:
        return "failed";
      default:
        return "idle";
    }
  }
  static const char *error_code(Error error) {
    switch (error) {
      case Error::NO_REFERENCE:
        return "no_reference";
      case Error::NO_ECHO:
        return "no_echo";
      case Error::WEAK_ECHO:
        return "weak_echo";
      case Error::INVALID_MUX:
        return "invalid_mux";
      case Error::XVF_IO:
        return "xvf_io";
      case Error::TIMEOUT:
        return "timeout";
      case Error::ABORTED:
        return "aborted";
      default:
        return "none";
    }
  }

  // --- XVF3800 side ---

  /// Values before the test, read during SETUP_A.
  void set_current_tuning(int sys_delay, float ref_gain) {
    results_.sys_delay_old = sys_delay;
    results_.sys_delay_new = sys_delay;
    results_.ref_gain_old = ref_gain;
    results_.ref_gain_new = ref_gain;
  }
  void set_converged(bool converged) {
    results_.converged_read = true;
    results_.converged = converged;
  }
  void action_done(bool ok) {
    const Action done = action_;
    action_ = Action::NONE;
    if (done == Action::RESTORE_ALL) {
      set_step_(Step::FAILED);
      return;
    }
    if (!ok && done != Action::READ_CONVERGED) {
      fail_(Error::XVF_IO);
      return;
    }
    switch (done) {
      case Action::SETUP_A:
      case Action::SETUP_ATTENUATION:
        reset_input_(ms_in(kSettleMs));
        set_step_(Step::FLOOR);
        break;
      case Action::SETUP_B:
        reset_input_(ms_in(kSettleMs));
        // Starts with silence, so the settle time swallows no noise.
        generator_.load(program_b_());
        set_step_(Step::MEASURE_B);
        break;
      case Action::READ_CONVERGED:
        finish_b_();
        break;
      case Action::RESTORE_MUX:
        set_step_(Step::DONE);
        break;
      default:
        break;
    }
  }

  // --- Audio ---

  /// Test signal for the jack, 48 kHz mono.
  size_t read_output(int16_t *out, size_t n) { return generator_.read(out, n); }

  /// XVF3800 output, 16 kHz, interleaved left and right.
  void write_input(const int16_t *frames, size_t count) {
    for (size_t i = 0; i < count; i++)
      process_frame_(frames[2 * i], frames[2 * i + 1]);
  }

  /// Bounded computation: the cross-correlation, in slices.
  void work() {
    if (step_ != Step::MEASURE_A)
      return;
    for (size_t b = 0; b < kBursts; b++) {
      Burst &burst = bursts_[b];
      if (!burst.captured || burst.correlated)
        continue;
      correlate_slice_(burst, b);
      return;
    }
    if (bursts_captured_ == kBursts && all_correlated_() && tones_done_ == kBands)
      finish_a_();
  }

 protected:
  struct Burst {
    int16_t *r{nullptr};  // kCorrN reference samples from the onset
    int16_t *l{nullptr};  // kCorrL microphone samples from kNegLag before it
    size_t r_fill{0};
    size_t l_fill{0};
    bool captured{false};
    bool correlated{false};
    int next_lag{0};
    double energy_r{0};
    double energy_l{0};  // sliding over the window at next_lag
    double best{0};
    int best_lag{0};
    int r_peak{0};
    int l_peak{0};
  };
  struct Decay {
    std::array<float, kDecayBlocks> energy{};
    uint32_t fill{0};
    bool active{false};
    bool done{false};
  };

  void set_step_(Step step) {
    step_ = step;
    step_frames_ = 0;
  }

  void fail_(Error error) {
    if (!running() || step_ == Step::FAILED)
      return;
    if (error_ == Error::NONE)
      error_ = error;
    generator_.reset(seed_);
    // Before SETUP_A ran, there is nothing to restore; RESTORE_ALL is safe.
    action_ = Action::RESTORE_ALL;
    set_step_(Step::RESTORE);
  }

  void reset_input_(uint32_t discard) {
    discard_ = discard;
    block_pos_ = 0;
    block_l_.fill(0.0);
    block_r_.fill(0.0);
    block_lw_ = block_rw_ = 0.0;
    for (size_t k = 0; k < kBands; k++) {
      left_bands_[k].reset();
      right_bands_[k].reset();
    }
    quiet_blocks_ = 0;
  }

  // --- Per frame ---

  void process_frame_(int16_t l, int16_t r) {
    if (!running() || action_ != Action::NONE)
      return;
    if (discard_ > 0) {
      discard_--;
      return;
    }
    input_frames_++;
    step_frames_++;

    if (step_ == Step::MEASURE_A) {
      if (std::abs(static_cast<int>(l)) >= 32500)
        results_.clipped = true;
      if (std::abs(static_cast<int>(r)) >= 32500)
        results_.ref_clipped = true;
    }

    if (step_ == Step::MEASURE_A)
      capture_burst_(l, r);

    // History for the samples before a burst onset.
    history_[history_pos_] = l;
    history_pos_ = (history_pos_ + 1) % kNegLag;

    const float lf = l, rf = r;
    for (size_t k = 0; k < kBands; k++) {
      const float lb = left_bands_[k].process(lf);
      const float rb = right_bands_[k].process(rf);
      block_l_[k] += static_cast<double>(lb) * lb;
      block_r_[k] += static_cast<double>(rb) * rb;
    }
    block_lw_ += static_cast<double>(lf) * lf;
    block_rw_ += static_cast<double>(rf) * rf;
    if (++block_pos_ < kBlock)
      return;
    for (size_t k = 0; k < kBands; k++) {
      block_l_[k] /= kBlock;
      block_r_[k] /= kBlock;
    }
    block_lw_ /= kBlock;
    block_rw_ /= kBlock;
    process_block_();
    block_pos_ = 0;
    block_l_.fill(0.0);
    block_r_.fill(0.0);
    block_lw_ = block_rw_ = 0.0;
  }

  void capture_burst_(int16_t l, int16_t r) {
    if (bursts_captured_ >= kBursts)
      return;
    Burst &burst = bursts_[bursts_captured_];
    if (!burst_open_) {
      // Armed after a quiet stretch, set in process_block_.
      if (!burst_armed_ || !pilot_over_ || std::abs(static_cast<int>(r)) < kOnsetLevel)
        return;
      burst_open_ = true;
      burst_armed_ = false;
      // The microphone window starts kNegLag samples before the onset.
      for (int i = 0; i < kNegLag; i++)
        burst.l[burst.l_fill++] = history_[(history_pos_ + i) % kNegLag];
      segment_frames_ = 0;
    }
    if (burst.r_fill < kCorrN) {
      burst.r[burst.r_fill++] = r;
      burst.r_peak = std::max(burst.r_peak, std::abs(static_cast<int>(r)));
    }
    if (burst.l_fill < kCorrL) {
      burst.l[burst.l_fill++] = l;
      burst.l_peak = std::max(burst.l_peak, std::abs(static_cast<int>(l)));
    }
    if (burst.r_fill == kCorrN && burst.l_fill == kCorrL) {
      burst.captured = true;
      burst_open_ = false;
      bursts_captured_++;
    }
  }

  // Onset of a burst in the reference channel: -40 dBFS as a sample value.
  static constexpr int kOnsetLevel = 328;

  // --- Per 10 ms block ---

  void process_block_() {
    const float r_db = ms_dbfs(block_rw_);
    const bool quiet = r_db < kRefQuietDbfs;
    const bool active = r_db > kRefActiveDbfs;
    quiet_blocks_ = quiet ? quiet_blocks_ + 1 : 0;

    switch (step_) {
      case Step::FLOOR:
        floor_block_();
        break;
      case Step::PILOT:
        pilot_block_(active);
        break;
      case Step::MEASURE_A:
        measure_a_block_(active);
        break;
      case Step::MEASURE_B:
        measure_b_block_();
        break;
      default:
        break;
    }
  }

  void floor_block_() {
    for (size_t k = 0; k < kBands; k++)
      floor_[k] += block_l_[k];
    floor_wide_ += block_lw_;
    floor_count_++;
    if (floor_count_ * kBlockMs < kFloorMs)
      return;
    for (size_t k = 0; k < kBands; k++)
      floor_[k] /= floor_count_;
    floor_wide_ /= floor_count_;
    results_.floor_dbfs = ms_dbfs(floor_wide_);
    if (mode_ == Mode::ATTENUATION) {
      // The reference channel carries the residual: the pilot's echo alone
      // tells that the speaker plays.
      generator_.load(program_b_());
      generator_.start_pilot();
      reference_seen_ = true;
      reference_frames_ = 0;
      set_step_(Step::PILOT);
      return;
    }
    std::vector<Generator::Segment> program;
    program.push_back({Generator::Kind::SILENCE, 0, ms_out(500)});
    for (size_t b = 0; b < kBursts; b++) {
      program.push_back({Generator::Kind::NOISE, 0, ms_out(kBurstMs)});
      program.push_back({Generator::Kind::SILENCE, 0, ms_out(kBurstGapMs)});
    }
    for (size_t k = 0; k < kBands; k++) {
      program.push_back({Generator::Kind::TONE, kBandHz[k], ms_out(kToneMs)});
      program.push_back({Generator::Kind::SILENCE, 0, ms_out(kDecayMs)});
    }
    generator_.load(program);
    generator_.start_pilot();
    set_step_(Step::PILOT);
  }

  void pilot_block_(bool active) {
    if (!reference_seen_) {
      if (active) {
        reference_seen_ = true;
        reference_frames_ = step_frames_;
      } else if (step_frames_ > ms_in(kNoReferenceMs)) {
        fail_(Error::NO_REFERENCE);
      }
      return;
    }
    if (block_l_[kPilotBand] > floor_[kPilotBand] * std::pow(10.0, kPilotEchoDb / 10.0)) {
      if (++pilot_echo_blocks_ >= kPilotEchoBlocks) {
        const uint32_t heard = step_frames_ - (kPilotEchoBlocks - 1) * kBlock;
        results_.echo_wait_ms = (heard > reference_frames_ ? heard - reference_frames_ : 0) * 1000.0f / kInRate;
        generator_.stop_pilot();
        set_step_(mode_ == Mode::FULL ? Step::MEASURE_A : Step::MEASURE_B);
        quiet_blocks_ = 0;
      }
    } else {
      pilot_echo_blocks_ = 0;
      if (step_frames_ - reference_frames_ > ms_in(kNoEchoMs))
        fail_(Error::NO_ECHO);
    }
  }

  void measure_a_block_(bool active) {
    // The pilot has ended once the reference is quiet.
    if (!pilot_over_) {
      if (quiet_blocks_ >= kQuietBlocks)
        pilot_over_ = true;
      else if (step_frames_ > ms_in(kSegmentTimeoutMs))
        fail_(Error::TIMEOUT);
      return;
    }
    if (bursts_captured_ < kBursts) {
      if (!burst_open_ && quiet_blocks_ >= kQuietBlocks)
        burst_armed_ = true;
      // Reset at every onset.
      segment_frames_ += kBlock;
      if (segment_frames_ > ms_in(kSegmentTimeoutMs + kBurstMs + kBurstGapMs))
        fail_(Error::TIMEOUT);
      return;
    }
    tone_block_(active);
    for (size_t k = 0; k < kBands; k++)
      decay_block_(k);
  }

  void tone_block_(bool active) {
    if (tones_started_ >= kBands && !tone_on_)
      return;
    segment_frames_ += kBlock;
    if (!tone_on_) {
      if (quiet_blocks_ >= kQuietBlocks)
        tone_armed_ = true;
      if (tone_armed_ && active) {
        tone_on_ = true;
        tone_armed_ = false;
        tone_blocks_ = 0;
        tone_band_ = tones_started_++;
        steady_l_ = steady_r_ = 0.0;
        steady_count_ = 0;
        segment_frames_ = 0;
      } else if (segment_frames_ > ms_in(kSegmentTimeoutMs)) {
        fail_(Error::TIMEOUT);
      }
      return;
    }
    tone_blocks_++;
    // Steady part: 500 to 1100 ms after the reference onset, clear of the
    // echo delay and of the ramps.
    if (tone_blocks_ > 50 && tone_blocks_ <= 110) {
      steady_l_ += block_l_[tone_band_];
      steady_r_ += block_r_[tone_band_];
      steady_count_++;
    }
    if (tone_blocks_ > 100 && quiet_blocks_ > 0) {
      // The reference has ended: the decay starts here, plus the echo delay.
      tone_on_ = false;
      BandResult &band = results_.bands[tone_band_];
      if (steady_count_ > 0) {
        steady_l_ /= steady_count_;
        steady_r_ /= steady_count_;
        band.ref_dbfs = ms_dbfs(steady_r_);
        band.echo_gain_db = to_db(steady_l_ / std::max(steady_r_, 1e-9));
        band.snr_db = to_db(steady_l_ / std::max(floor_[tone_band_], 1e-9));
      }
      Decay &decay = decays_[tone_band_];
      decay.active = true;
      decay.fill = 0;
      segment_frames_ = 0;
    } else if (tone_blocks_ > (kToneMs + kSegmentTimeoutMs) / kBlockMs) {
      fail_(Error::TIMEOUT);
    }
  }

  void decay_block_(size_t k) {
    Decay &decay = decays_[k];
    if (!decay.active)
      return;
    decay.energy[decay.fill++] = static_cast<float>(block_l_[k]);
    if (decay.fill >= kDecayBlocks) {
      decay.active = false;
      decay.done = true;
      tones_done_++;
    }
  }

  // Phase B: 0.5 s silence, the noise, a short silence.
  static std::vector<Generator::Segment> program_b_() {
    return {{Generator::Kind::SILENCE, 0, ms_out(500)},
            {Generator::Kind::NOISE, 0, ms_out(kConvergeMs)},
            {Generator::Kind::SILENCE, 0, ms_out(300)}};
  }

  void measure_b_block_() {
    if (!onset_b_) {
      const double floor = floor_wide_ * std::pow(10.0, kOnsetBDb / 10.0);
      // Echo still ringing from the pilot or phase A: wait for quiet first.
      if (!quiet_b_) {
        quiet_b_ = block_lw_ < floor;
      } else if (block_lw_ > floor) {
        onset_b_ = true;
        onset_b_blocks_ = 0;
      }
      if (!onset_b_ && step_frames_ > ms_in(kOnsetBTimeoutMs))
        fail_(Error::NO_ECHO);
      return;
    }
    onset_b_blocks_++;
    for (size_t w = 0; w < kErleWindows; w++) {
      if (onset_b_blocks_ > kErleWindowStart[w] && onset_b_blocks_ <= kErleWindowEnd[w]) {
        window_l_[w] += block_lw_;
        window_r_[w] += block_rw_;
      }
    }
    // Per band over the last window, once the canceller has adapted.
    if (onset_b_blocks_ > kErleWindowStart[kErleWindows - 1] && onset_b_blocks_ <= kErleWindowEnd[kErleWindows - 1]) {
      for (size_t k = 0; k < kBands; k++) {
        erle_l_[k] += block_l_[k];
        erle_r_[k] += block_r_[k];
      }
    }
    if (onset_b_blocks_ == kErleWindowEnd[kErleWindows - 1]) {
      set_step_(Step::READ_CONVERGED);
      action_ = Action::READ_CONVERGED;
    }
  }

  // --- Cross-correlation ---

  void correlate_slice_(Burst &burst, size_t index) {
    if (burst.next_lag == 0) {
      burst.energy_r = 0.0;
      burst.energy_l = 0.0;
      for (size_t n = 0; n < kCorrN; n++) {
        burst.energy_r += static_cast<double>(burst.r[n]) * burst.r[n];
        burst.energy_l += static_cast<double>(burst.l[n]) * burst.l[n];
      }
      burst.best = 0.0;
      burst.best_lag = 0;
    }
    const int end = std::min(kLags, burst.next_lag + kLagsPerWork);
    for (int lag = burst.next_lag; lag < end; lag++) {
      int64_t sum = 0;
      const int16_t *r = burst.r;
      const int16_t *l = burst.l + lag;
      for (size_t n = 0; n < kCorrN; n++)
        sum += static_cast<int32_t>(r[n]) * l[n];
      const double norm = std::sqrt(burst.energy_r * burst.energy_l);
      const double coeff = norm > 0.0 ? std::fabs(static_cast<double>(sum)) / norm : 0.0;
      if (coeff > burst.best) {
        burst.best = coeff;
        burst.best_lag = lag;
      }
      // Slide the microphone energy to the next lag.
      const double out = burst.l[lag];
      const double in = lag + kCorrN < kCorrL ? burst.l[lag + kCorrN] : 0.0;
      burst.energy_l += in * in - out * out;
      if (burst.energy_l < 0.0)
        burst.energy_l = 0.0;
    }
    burst.next_lag = end;
    if (end >= kLags) {
      burst.correlated = true;
      results_.burst_delay[index] = burst.best_lag - kNegLag;
      results_.burst_coeff[index] = static_cast<float>(burst.best);
    }
  }

  bool all_correlated_() const {
    for (const auto &burst : bursts_)
      if (!burst.correlated)
        return false;
    return true;
  }

  // --- Results ---

  void finish_a_() {
    Results &res = results_;
    std::vector<float> delays;
    for (size_t b = 0; b < kBursts; b++)
      if (res.burst_coeff[b] >= kMinCoeff)
        delays.push_back(static_cast<float>(res.burst_delay[b]));
    res.valid_bursts = static_cast<int>(delays.size());
    if (res.valid_bursts < 2) {
      fail_(Error::WEAK_ECHO);
      return;
    }
    res.delay_samples = static_cast<int>(std::lround(median(delays)));
    res.delay_spread = static_cast<int>(*std::max_element(delays.begin(), delays.end()) -
                                        *std::min_element(delays.begin(), delays.end()));
    res.delay_ms = res.delay_samples * 1000.0f / kInRate;
    // The echo of the first audio the speaker played arrives one echo delay
    // later; the 10 ms blocks round the rest.
    res.switch_ms = std::max(0.0f, res.echo_wait_ms - res.delay_ms);

    if (res.delay_spread > kMaxSpread) {
      res.warnings |= WARN_DELAY_UNSTABLE;
    } else {
      const int wanted = res.delay_samples - kDelayMargin;
      res.sys_delay_new = std::max(kSysDelayMin, std::min(kSysDelayMax, wanted));
      if (wanted > kSysDelayMax)
        res.warnings |= WARN_DELAY_CLAMPED;
    }
    res.residual_ms = (res.delay_samples - res.sys_delay_new) * 1000.0f / kInRate;
    res.usable_tail_ms = std::max(0.0f, kAecTailMs - std::max(0.0f, res.residual_ms));

    // Levels over the correlation windows of the valid bursts; the
    // microphone window is aligned to the echo.
    double ref_ms = 0, mic_ms = 0;
    int ref_peak = 0, mic_peak = 0, used = 0;
    const int offset = std::max(0, std::min(kLags - 1, res.delay_samples + kNegLag));
    for (size_t b = 0; b < kBursts; b++) {
      if (res.burst_coeff[b] < kMinCoeff)
        continue;
      const Burst &burst = bursts_[b];
      double r2 = 0, l2 = 0;
      for (size_t n = 0; n < kCorrN; n++) {
        r2 += static_cast<double>(burst.r[n]) * burst.r[n];
        l2 += static_cast<double>(burst.l[n + offset]) * burst.l[n + offset];
      }
      ref_ms += r2 / kCorrN;
      mic_ms += l2 / kCorrN;
      ref_peak = std::max(ref_peak, burst.r_peak);
      mic_peak = std::max(mic_peak, burst.l_peak);
      used++;
    }
    res.ref_rms_dbfs = ms_dbfs(ref_ms / used);
    res.mic_rms_dbfs = ms_dbfs(mic_ms / used);
    // Peaks as dBFS of a sample: 20 log10(|x| / 32768), comparable to the RMS
    // values of a full-scale square.
    res.ref_peak_dbfs = to_db(static_cast<double>(ref_peak) * ref_peak / (32768.0 * 32768.0));
    res.mic_peak_dbfs = to_db(static_cast<double>(mic_peak) * mic_peak / (32768.0 * 32768.0));

    // Reference gain: a full-scale signal should reach kRefTargetDbfs. The
    // tones have a known level, so the gain of the reference path (measured
    // at kMeasureRefGain) follows from their steady level. (The noise is no
    // measure: the XVF3800 drops its share above 8 kHz.)
    std::vector<float> path;
    for (size_t k = 2; k <= 4; k++)
      path.push_back(res.bands[k].ref_dbfs - tone_dbfs(kTonePeak));
    const float path_db = median(path);
    if (res.ref_clipped) {
      res.warnings |= WARN_REF_CLIPPED;
    } else if (std::isfinite(path_db) && std::isfinite(res.ref_gain_old) && res.ref_gain_old > 0.0f) {
      const float wanted = std::max(
          kRefGainMin, std::min(kRefGainMax, kMeasureRefGain * std::pow(10.0f, (kRefTargetDbfs - path_db) / 20.0f)));
      if (std::fabs(20.0f * std::log10(wanted / res.ref_gain_old)) >= kRefGainStepDb)
        res.ref_gain_new = wanted;
    }
    // The reference was measured at kMeasureRefGain; compare with the gain
    // that stays.
    const float gain_db = std::isfinite(res.ref_gain_new) && res.ref_gain_new > 0.0f
                              ? 20.0f * std::log10(res.ref_gain_new / kMeasureRefGain)
                              : 0.0f;
    res.echo_to_ref_db = res.mic_peak_dbfs - (res.ref_peak_dbfs + gain_db);
    if (res.echo_to_ref_db > kEchoToRefMaxDb)
      res.warnings |= WARN_ECHO_LOUD;
    if (res.clipped)
      res.warnings |= WARN_CLIPPING;

    // Reverberation per band, from the first block after the direct echo of
    // the reference's end.
    const size_t skip = (static_cast<size_t>(std::max(0, res.delay_samples)) + kBlock - 1) / kBlock + 1;
    for (size_t k = 0; k < kBands; k++) {
      BandResult &band = res.bands[k];
      const Decay &decay = decays_[k];
      const size_t first = std::min<size_t>(skip, kDecayBlocks - kDecayUsable);
      band.rt60_ms =
          reverberation_ms(decay.energy.data() + first, kDecayUsable, static_cast<float>(floor_[k]), band.rt_range_db);
      if (band.rt_range_db == 0)
        res.warnings |= WARN_LOW_SNR;
    }
    res.rt60_ms = median({res.bands[2].rt60_ms, res.bands[3].rt60_ms, res.bands[4].rt60_ms});
    if (std::isnan(res.rt60_ms)) {
      std::vector<float> all;
      for (const auto &band : res.bands)
        all.push_back(band.rt60_ms);
      res.rt60_ms = median(all);
    }
    if (std::isfinite(res.rt60_ms) && res.rt60_ms > 0.0f) {
      res.tail_coverage_db = std::min(60.0f, 60.0f * res.usable_tail_ms / res.rt60_ms);
      if (res.tail_coverage_db < 20.0f)
        res.warnings |= WARN_REVERB_LONG;
    }

    set_step_(Step::SETUP_B);
    action_ = Action::SETUP_B;
  }

  void finish_b_() {
    Results &res = results_;
    for (size_t w = 0; w < kErleWindows; w++)
      res.erle_trend[w] = to_db(window_l_[w] / std::max(window_r_[w], 1e-9));
    // An adapted canceller never leaves more than the microphone had; while
    // it adapts it can, so only the last window counts.
    const size_t last = kErleWindows - 1;
    // Without the noise's echo both channels carry only room noise.
    const double blocks = kErleWindowEnd[last] - kErleWindowStart[last];
    if (window_l_[last] / blocks < floor_wide_ * std::pow(10.0, kOnsetBDb / 10.0)) {
      fail_(Error::NO_ECHO);
      return;
    }
    if (window_r_[last] > 2.0 * window_l_[last]) {
      fail_(Error::INVALID_MUX);
      return;
    }
    res.erle_db = res.erle_trend[last];
    std::vector<float> bands;
    for (size_t k = 0; k < kBands; k++) {
      res.bands[k].erle_db = to_db(erle_l_[k] / std::max(erle_r_[k], 1e-9));
      bands.push_back(res.bands[k].erle_db);
    }
    res.erle_band_db = median(bands);
    if (res.erle_db < 10.0f)
      res.warnings |= WARN_LOW_ERLE;
    if (res.converged_read && !res.converged)
      res.warnings |= WARN_NOT_CONVERGED;
    set_step_(Step::RESTORE);
    action_ = Action::RESTORE_MUX;
  }

  uint32_t seed_{0};
  Mode mode_{Mode::FULL};
  Step step_{Step::IDLE};
  Error error_{Error::NONE};
  Action action_{Action::NONE};
  Results results_{};
  Generator generator_{};

  uint64_t input_frames_{0};
  uint32_t step_frames_{0};
  uint32_t discard_{0};

  std::array<BandPass, kBands> left_bands_{};
  std::array<BandPass, kBands> right_bands_{};
  std::array<double, kBands> block_l_{};
  std::array<double, kBands> block_r_{};
  double block_lw_{0}, block_rw_{0};
  uint32_t block_pos_{0};
  uint32_t quiet_blocks_{0};

  std::array<double, kBands> floor_{};
  double floor_wide_{0};
  uint32_t floor_count_{0};

  bool reference_seen_{false};
  uint32_t reference_frames_{0};
  uint32_t pilot_echo_blocks_{0};
  bool pilot_over_{false};

  std::array<int16_t, kNegLag> history_{};
  size_t history_pos_{0};
  int16_t *buffer_{nullptr};
  std::array<Burst, kBursts> bursts_{};
  size_t bursts_captured_{0};
  bool burst_open_{false};
  bool burst_armed_{false};
  uint32_t segment_frames_{0};

  size_t tones_started_{0};
  size_t tones_done_{0};
  bool tone_on_{false};
  bool tone_armed_{false};
  size_t tone_band_{0};
  uint32_t tone_blocks_{0};
  double steady_l_{0}, steady_r_{0};
  uint32_t steady_count_{0};
  std::array<Decay, kBands> decays_{};

  bool quiet_b_{false};
  bool onset_b_{false};
  uint32_t onset_b_blocks_{0};
  std::array<double, kBands> erle_l_{};
  std::array<double, kBands> erle_r_{};
  std::array<double, kErleWindows> window_l_{};
  std::array<double, kErleWindows> window_r_{};
};

}  // namespace aec_self_test
