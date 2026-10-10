// Room simulator for the echo canceller self test engine
// (esphome/components/aec_tuning/analysis.h).
//
// It plays the engine's test signal into a simulated output path and room
// and feeds the engine what the XVF3800 would send back: the microphone after
// gain on the left, the reference or the echo canceller residual on the
// right. The engine's XVF3800 actions change the simulated mux and settings,
// as the aec_tuning component does on the device. Prints the results as JSON.
//
//   room_simulator [--delay N] [--rt60 MS] [--echo-db DB] [--erle DB]
//                  [--noise-dbfs DB] [--ref-gain G] [--sys-delay N] [--ref-path-db DB]
//                  [--latency-ms MS] [--switch-ms MS] [--unstable]
//                  [--no-reference] [--no-echo] [--abort-ms MS]

#include "analysis.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <random>
#include <string>
#include <vector>

using namespace aec_self_test;

namespace {

struct Options {
  int delay = 880;            // echo behind the reference, samples at 16 kHz
  float rt60_ms = 400.0f;
  float echo_db = -18.0f;     // direct echo at the microphone, relative to the reference
  float erle_db = 20.0f;
  float noise_dbfs = -60.0f;  // microphone noise, RMS
  float ref_gain = 8.0f;      // AUDIO_MGR_REF_GAIN before the test (formatBCE firmware)
  int sys_delay = -30;
  float ref_path_db = 0.0f;   // gain from the generator to the reference, before REF_GAIN
  float latency_ms = 250.0f;  // generator to reference
  float switch_ms = 0.0f;     // speaker silent this long after the first reference
  bool unstable = false;      // the echo delay grows by 80 samples per burst
  bool no_reference = false;  // playback never reaches the XVF3800
  bool no_echo = false;       // the speaker does not play the jack
  float abort_ms = -1.0f;
};

Options parse(int argc, char **argv) {
  Options o;
  for (int i = 1; i < argc; i++) {
    const std::string arg = argv[i];
    auto next = [&]() { return i + 1 < argc ? std::atof(argv[++i]) : 0.0; };
    if (arg == "--delay") o.delay = static_cast<int>(next());
    else if (arg == "--rt60") o.rt60_ms = next();
    else if (arg == "--echo-db") o.echo_db = next();
    else if (arg == "--erle") o.erle_db = next();
    else if (arg == "--noise-dbfs") o.noise_dbfs = next();
    else if (arg == "--ref-gain") o.ref_gain = next();
    else if (arg == "--sys-delay") o.sys_delay = static_cast<int>(next());
    else if (arg == "--latency-ms") o.latency_ms = next();
    else if (arg == "--ref-path-db") o.ref_path_db = next();
    else if (arg == "--switch-ms") o.switch_ms = next();
    else if (arg == "--abort-ms") o.abort_ms = next();
    else if (arg == "--unstable") o.unstable = true;
    else if (arg == "--no-reference") o.no_reference = true;
    else if (arg == "--no-echo") o.no_echo = true;
    else {
      std::fprintf(stderr, "unknown option %s\n", arg.c_str());
      std::exit(2);
    }
  }
  return o;
}

/// 48 kHz to 16 kHz: windowed sinc low pass at 7 kHz, every third sample.
class Decimator {
 public:
  Decimator() {
    const int n = kTaps;
    double sum = 0;
    for (int i = 0; i < n; i++) {
      const double m = i - (n - 1) / 2.0;
      const double fc = 7000.0 / 48000.0;
      const double sinc = m == 0 ? 2 * fc : std::sin(2 * M_PI * fc * m) / (M_PI * m);
      const double window = 0.54 - 0.46 * std::cos(2 * M_PI * i / (n - 1));
      taps_[i] = sinc * window;
      sum += taps_[i];
    }
    for (double &t : taps_)
      t /= sum;
  }
  /// Pushes three samples, returns one.
  double push3(const float *x) {
    for (int k = 0; k < 3; k++) {
      history_[pos_] = x[k];
      pos_ = (pos_ + 1) % kTaps;
    }
    double y = 0;
    for (int i = 0; i < kTaps; i++)
      y += taps_[i] * history_[(pos_ + i) % kTaps];
    return y;
  }

 private:
  static constexpr int kTaps = 63;
  double taps_[kTaps]{};
  double history_[kTaps]{};
  int pos_ = 0;
};

/// Schroeder reverberator: eight parallel feedback combs with the same decay
/// time, then two all-pass diffusers.
class Reverb {
 public:
  Reverb(float rt60_ms) {
    const int delays[kCombs] = {557, 617, 491, 422, 677, 733, 389, 523};
    for (int i = 0; i < kCombs; i++) {
      lines_[i].assign(delays[i], 0.0);
      gains_[i] = std::pow(10.0, -3.0 * delays[i] / (rt60_ms / 1000.0 * kInRate));
    }
    allpass_[0].assign(113, 0.0);
    allpass_[1].assign(37, 0.0);
  }
  double process(double x) {
    double y = 0;
    for (int i = 0; i < kCombs; i++) {
      std::vector<double> &line = lines_[i];
      const double out = line[pos_[i]];
      line[pos_[i]] = x + gains_[i] * out;
      pos_[i] = (pos_[i] + 1) % line.size();
      y += out;
    }
    y /= kCombs;
    for (int i = 0; i < 2; i++) {
      std::vector<double> &line = allpass_[i];
      const double delayed = line[ap_pos_[i]];
      const double in = y + 0.7 * delayed;
      line[ap_pos_[i]] = in;
      ap_pos_[i] = (ap_pos_[i] + 1) % line.size();
      y = delayed - 0.7 * in;
    }
    return y;
  }

 private:
  static constexpr int kCombs = 8;
  std::vector<double> lines_[kCombs];
  double gains_[kCombs]{};
  size_t pos_[kCombs]{};
  std::vector<double> allpass_[2];
  size_t ap_pos_[2]{};
};

int16_t clip16(double x) {
  if (x > 32767) return 32767;
  if (x < -32768) return -32768;
  return static_cast<int16_t>(std::lround(x));
}

}  // namespace

int main(int argc, char **argv) {
  const Options o = parse(argc, argv);

  std::vector<int16_t> buffer(kBufferSamples);
  Engine engine;
  engine.set_buffer(buffer.data());
  engine.start();

  // Simulated XVF3800 state.
  enum class Mux { DEFAULT, PHASE_A, PHASE_B } mux = Mux::DEFAULT;
  int sys_delay = o.sys_delay;
  float ref_gain = o.ref_gain;
  bool ever_restored = false;

  Decimator decimator;
  Reverb reverb(o.rt60_ms);
  std::mt19937 rng(7);
  std::normal_distribution<double> gauss(0.0, 1.0);
  const double noise_rms = 32768.0 * std::pow(10.0, o.noise_dbfs / 20.0);
  const double echo_gain = std::pow(10.0, o.echo_db / 20.0);
  const double residual_gain = std::pow(10.0, -o.erle_db / 20.0);
  const double ref_path = std::pow(10.0, o.ref_path_db / 20.0);

  const size_t latency = static_cast<size_t>(o.latency_ms * kInRate / 1000.0f);
  std::deque<double> output_path(latency, 0.0);  // generator at 16 kHz, before the XVF3800
  std::vector<double> reference_history;         // reference before gain, for the echo
  reference_history.reserve(1 << 20);

  long first_reference = -1;
  long input_index = 0;
  const long max_frames = 180L * kInRate;
  int16_t out48[480];
  int16_t frames[2 * 160];

  while (engine.running() && input_index < max_frames) {
    // XVF3800 actions.
    switch (engine.action()) {
      case Action::SETUP_A:
        engine.set_current_tuning(sys_delay, ref_gain);
        sys_delay = 0;
        ref_gain = kMeasureRefGain;
        mux = Mux::PHASE_A;
        engine.action_done(true);
        break;
      case Action::SETUP_B:
        sys_delay = engine.results().sys_delay_new;
        ref_gain = engine.results().ref_gain_new;
        mux = Mux::PHASE_B;
        engine.action_done(true);
        break;
      case Action::READ_CONVERGED:
        engine.set_converged(true);
        engine.action_done(true);
        break;
      case Action::RESTORE_MUX:
        mux = Mux::DEFAULT;
        engine.action_done(true);
        break;
      case Action::RESTORE_ALL:
        mux = Mux::DEFAULT;
        sys_delay = o.sys_delay;
        ref_gain = o.ref_gain;
        ever_restored = true;
        engine.action_done(true);
        break;
      default:
        break;
    }
    if (!engine.running())
      break;

    // 10 ms of output.
    const size_t got = engine.read_output(out48, 480);
    std::memset(out48 + got, 0, (480 - got) * sizeof(int16_t));
    float x48[480];
    for (int i = 0; i < 480; i++)
      x48[i] = out48[i];

    for (int i = 0; i < 160; i++) {
      output_path.push_back(decimator.push3(x48 + 3 * i));
      const double reference = o.no_reference ? 0.0 : output_path.front();
      output_path.pop_front();
      reference_history.push_back(reference);
      if (first_reference < 0 && std::fabs(reference) > 30.0)
        first_reference = input_index;

      // The echo: direct path plus reverberation, delay D.
      int delay = o.delay;
      if (o.unstable && first_reference >= 0)
        delay += 80 * static_cast<int>((input_index - first_reference) / (kInRate * 1.2));
      const long source = input_index - delay;
      double played = source >= 0 ? reference_history[source] : 0.0;
      // The speaker plays its input only from the switch on; what it plays
      // reaches the microphone one echo delay later.
      const bool speaker_on = !o.no_echo && !o.no_reference &&
                              (first_reference < 0 ||
                               source - first_reference >= o.switch_ms * kInRate / 1000.0f);
      if (!speaker_on)
        played = 0.0;
      const double room = echo_gain * (played + 0.6 * reverb.process(played));
      const double mic = room + noise_rms * gauss(rng);

      double left = mic, right = 0.0;
      switch (mux) {
        case Mux::PHASE_A:
          right = reference * ref_path * ref_gain;
          break;
        case Mux::PHASE_B:
          right = room * residual_gain + noise_rms * gauss(rng);
          break;
        default:
          right = mic;  // processed output, irrelevant here
          break;
      }
      frames[2 * i] = clip16(left);
      frames[2 * i + 1] = clip16(right);
      input_index++;
    }
    engine.write_input(frames, 160);
    engine.work();

    if (o.abort_ms >= 0 && input_index >= o.abort_ms * kInRate / 1000.0f) {
      engine.abort();
    }
  }
  if (engine.running())
    engine.fail(Error::TIMEOUT);
  // Carry out the restore a failure asked for.
  if (engine.action() == Action::RESTORE_ALL) {
    mux = Mux::DEFAULT;
    sys_delay = o.sys_delay;
    ref_gain = o.ref_gain;
    ever_restored = true;
    engine.action_done(true);
  }

  const Results &r = engine.results();
  auto num = [](float v) {
    char text[32];
    if (std::isfinite(v))
      std::snprintf(text, sizeof(text), "%.3f", v);
    else
      std::snprintf(text, sizeof(text), "null");
    return std::string(text);
  };
  std::printf("{\"step\":\"%s\",\"error\":\"%s\",\"warnings\":%u,", Engine::step_name(engine.step()),
              Engine::error_code(engine.error()), r.warnings);
  std::printf("\"delay_samples\":%d,\"delay_spread\":%d,\"valid_bursts\":%d,", r.delay_samples, r.delay_spread,
              r.valid_bursts);
  std::printf("\"burst_delay\":[%d,%d,%d],\"burst_coeff\":[%s,%s,%s],", r.burst_delay[0], r.burst_delay[1],
              r.burst_delay[2], num(r.burst_coeff[0]).c_str(), num(r.burst_coeff[1]).c_str(),
              num(r.burst_coeff[2]).c_str());
  std::printf("\"sys_delay_old\":%d,\"sys_delay_new\":%d,\"ref_gain_old\":%s,\"ref_gain_new\":%s,", r.sys_delay_old,
              r.sys_delay_new, num(r.ref_gain_old).c_str(), num(r.ref_gain_new).c_str());
  std::printf("\"final_sys_delay\":%d,\"final_ref_gain\":%s,\"restored\":%s,", sys_delay, num(ref_gain).c_str(),
              ever_restored ? "true" : "false");
  std::printf("\"echo_wait_ms\":%s,\"switch_ms\":%s,\"residual_ms\":%s,\"usable_tail_ms\":%s,",
              num(r.echo_wait_ms).c_str(), num(r.switch_ms).c_str(), num(r.residual_ms).c_str(),
              num(r.usable_tail_ms).c_str());
  std::printf("\"ref_peak_dbfs\":%s,\"mic_peak_dbfs\":%s,\"echo_to_ref_db\":%s,\"clipped\":%s,"
              "\"ref_clipped\":%s,",
              num(r.ref_peak_dbfs).c_str(), num(r.mic_peak_dbfs).c_str(), num(r.echo_to_ref_db).c_str(),
              r.clipped ? "true" : "false", r.ref_clipped ? "true" : "false");
  std::printf("\"rt60_ms\":%s,\"tail_coverage_db\":%s,\"erle_db\":%s,\"erle_band_db\":%s,\"converged\":%s,",
              num(r.rt60_ms).c_str(), num(r.tail_coverage_db).c_str(), num(r.erle_db).c_str(),
              num(r.erle_band_db).c_str(), r.converged ? "true" : "false");
  std::printf("\"bands\":[");
  for (size_t k = 0; k < kBands; k++) {
    const BandResult &b = r.bands[k];
    std::printf("%s{\"hz\":%.0f,\"ref_dbfs\":%s,\"echo_gain_db\":%s,\"snr_db\":%s,\"rt60_ms\":%s,\"rt_range_db\":%u,"
                "\"erle_db\":%s}",
                k ? "," : "", kBandHz[k], num(b.ref_dbfs).c_str(), num(b.echo_gain_db).c_str(),
                num(b.snr_db).c_str(), num(b.rt60_ms).c_str(), b.rt_range_db, num(b.erle_db).c_str());
  }
  std::printf("],\"input_ms\":%u}\n", engine.input_ms());
  return 0;
}
