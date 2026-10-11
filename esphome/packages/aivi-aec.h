// AIVI satellite: texts for the echo canceller self test
//
// Used by aivi-aec.yaml. Turns the aec_tuning results into the German texts
// of the Home Assistant entities. Home Assistant keeps at most 255
// characters per state.
#pragma once

#include <cmath>
#include <cstdio>
#include <string>

#include "esphome/components/aec_tuning/analysis.h"

namespace aivi_aec {

using aec_self_test::Results;

constexpr size_t kMaxState = 255;

/// Cut to Home Assistant's limit without splitting a UTF-8 character.
inline std::string fit(std::string text) {
  if (text.size() <= kMaxState)
    return text;
  size_t end = kMaxState;
  while (end > 0 && (static_cast<unsigned char>(text[end]) & 0xC0) == 0x80)
    end--;
  text.resize(end);
  return text;
}

/// A number, or "–" if it is unknown.
inline std::string num(float value, int decimals = 0) {
  if (!std::isfinite(value))
    return "–";
  char text[24];
  snprintf(text, sizeof(text), "%.*f", decimals, value);
  return text;
}

inline std::string status_text(const std::string &status) {
  if (status == "running:setup") return "Läuft: Vorbereitung";
  if (status == "running:noise_floor") return "Läuft: Ruhepegel";
  if (status == "running:pilot") return "Läuft: Warte auf das Echo vom Analogeingang";
  if (status == "running:warmup") return "Läuft: Sweep, Lautsprecher schaltet auf den Analogeingang";
  if (status == "running:delay_and_tones") return "Läuft: Verzögerung, Töne und Nachhall";
  if (status == "running:apply") return "Läuft: Werte anwenden";
  if (status == "running:attenuation") return "Läuft: Echodämpfung";
  if (status == "running:finish") return "Läuft: Abschluss";
  if (status == "done") return "Fertig";
  if (status == "error:no_reference") return "Fehler: Die Wiedergabe erreicht den XVF3800 nicht";
  if (status == "error:no_echo")
    return "Fehler: Kein Echo. Lautsprecher auf Analogeingang, Kabel und Lautstärke prüfen";
  if (status == "error:weak_echo") return "Fehler: Echo zu schwach für die Verzögerung";
  if (status == "error:invalid_mux") return "Fehler: Messkanäle unplausibel";
  if (status == "error:xvf_io") return "Fehler: XVF3800 antwortet nicht";
  if (status == "error:timeout") return "Fehler: Zeitüberschreitung";
  if (status == "error:aborted") return "Abgebrochen";
  if (status == "error:memory") return "Fehler: Zu wenig Speicher";
  return "Fehler: " + status;
}

/// "a / b / c / d dB" of the attenuation after 5, 10, 15 and 20 s.
inline std::string erle_trend(const Results &r) {
  std::string text;
  for (size_t w = 0; w < aec_self_test::kErleWindows; w++)
    text += (w ? " / " : "") + num(r.erle_trend[w], 1);
  return text + " dB";
}

/// Test of the script parameter: "full", "attenuation" or "warmup".
inline aec_self_test::Engine::Mode test_mode(const std::string &test) {
  if (test == "attenuation")
    return aec_self_test::Engine::Mode::ATTENUATION;
  if (test == "warmup")
    return aec_self_test::Engine::Mode::WARMUP;
  return aec_self_test::Engine::Mode::FULL;
}

/// Comma separated values for events, "–" for unknown ones.
template<typename F> std::string list(size_t count, F value) {
  std::string text;
  for (size_t i = 0; i < count; i++)
    text += (i ? "," : "") + value(i);
  return text;
}

/// Warm-up test: ms from the switch to full level, per band.
inline std::string warmup_band_ready(const Results &r) {
  return list(aec_self_test::kBands, [&r](size_t k) { return num(r.bands[k].ready_ms); });
}
/// Warm-up test: final echo level per band, dBFS.
inline std::string warmup_band_levels(const Results &r) {
  return list(aec_self_test::kBands, [&r](size_t k) { return num(r.bands[k].level_dbfs, 1); });
}
/// Warm-up test: broadband level per sweep relative to the final level, dB.
inline std::string warmup_ramp(const Results &r) {
  return list(r.warmup_windows, [&r](size_t w) { return std::to_string(r.warmup_ramp_db[w]); });
}

/// The main values in one line.
inline std::string summary(const Results &r) {
  if (r.warmup) {
    std::string text = "Ab dem Umschaltbefehl: erster Ton nach " + num(r.warmup_onset_ms / 1000.0f, 2) + " s";
    text += ", volle Lautstärke (±1 dB) nach " + num(r.warmup_ready_ms / 1000.0f, 2) + " s";
    text += ", Sweep alle " + std::to_string(aec_self_test::kSweepMs) + " ms";
    if (!r.warmup_settled)
      text += ", Pegel am Ende nicht stabil";
    return fit(text);
  }
  if (r.attenuation_only) {
    std::string text = "Nur gemessen, Werte unverändert: Dämpfung nach 5/10/15/20 s " + erle_trend(r);
    text += ", SYS_DELAY " + std::to_string(r.sys_delay_old) + ", REF_GAIN " + num(r.ref_gain_old, 2);
    if (r.converged_read)
      text += r.converged ? ", eingeschwungen" : ", nicht eingeschwungen";
    // Normal is about half a second from the pilot's start to its echo.
    if (std::isfinite(r.echo_wait_ms) && r.echo_wait_ms >= 1500.0f)
      text += ", Lautsprecher spielte erst nach " + num(r.echo_wait_ms / 1000.0f, 1) + " s";
    return fit(text);
  }
  std::string text = "Echo " + num(r.delay_ms, 1) + " ms";
  text += " (Streuung " + std::to_string(r.delay_spread) + " Samples)";
  text += ", SYS_DELAY " + std::to_string(r.sys_delay_old) + " → " + std::to_string(r.sys_delay_new);
  text += ", Rest " + num(r.residual_ms) + " ms";
  text += ", REF_GAIN " + num(r.ref_gain_old, 2) + " → " + num(r.ref_gain_new, 2);
  text += ", Echo zur Referenz " + num(r.echo_to_ref_db, 1) + " dB";
  text += ", RT60 " + num(r.rt60_ms) + " ms";
  text += ", Filter deckt " + num(r.tail_coverage_db) + " dB Nachhall";
  text += ", Dämpfung nach 5/10/15/20 s " + erle_trend(r);
  if (r.converged_read)
    text += r.converged ? ", eingeschwungen" : ", nicht eingeschwungen";
  // The room speaker started playing the jack only after the pilot began.
  if (std::isfinite(r.switch_ms) && r.switch_ms >= 100.0f)
    text += ", Lautsprecher spielte erst nach " + num(r.switch_ms / 1000.0f, 1) + " s";
  return fit(text);
}

/// Per band: echo path gain, reverberation time, echo attenuation.
inline std::string bands(const Results &r) {
  if (r.warmup) {
    std::string text = "Volle Lautstärke ab s / Pegel dBFS:";
    for (size_t k = 0; k < aec_self_test::kBands; k++) {
      const float hz = aec_self_test::kBandHz[k];
      text += k ? "; " : " ";
      text += hz >= 1000.0f ? num(hz / 1000.0f) + " kHz " : num(hz) + " Hz ";
      text += num(r.bands[k].ready_ms / 1000.0f, 2) + "/" + num(r.bands[k].level_dbfs);
    }
    return fit(text);
  }
  if (r.attenuation_only) {
    std::string text = "Dämpfung dB:";
    for (size_t k = 0; k < aec_self_test::kBands; k++) {
      const float hz = aec_self_test::kBandHz[k];
      text += k ? "; " : " ";
      text += (hz >= 1000.0f ? num(hz / 1000.0f) + " kHz " : num(hz) + " Hz ") + num(r.bands[k].erle_db);
    }
    return fit(text);
  }
  std::string text = "Echo dB / RT60 ms / Dämpfung dB:";
  for (size_t k = 0; k < aec_self_test::kBands; k++) {
    const aec_self_test::BandResult &band = r.bands[k];
    const float hz = aec_self_test::kBandHz[k];
    text += k ? "; " : " ";
    text += hz >= 1000.0f ? num(hz / 1000.0f) + " kHz " : num(hz) + " Hz ";
    text += num(band.echo_gain_db) + "/" + num(band.rt60_ms) + "/" + num(band.erle_db);
  }
  return fit(text);
}

/// What to do about the result; "Keine" if nothing.
inline std::string hints(const Results &r) {
  std::string text;
  auto add = [&text](const char *hint) {
    if (!text.empty())
      text += "; ";
    text += hint;
  };
  if (r.warnings & aec_self_test::WARN_DELAY_UNSTABLE)
    add("Verzögerung schwankt, SYS_DELAY bleibt");
  if (r.warnings & aec_self_test::WARN_DELAY_CLAMPED)
    add("Verzögerung länger als SYS_DELAY ausgleicht");
  if (r.warnings & aec_self_test::WARN_CLIPPING)
    add("Mikrofon übersteuert: Audioausgang leiser");
  if (r.warnings & aec_self_test::WARN_ECHO_LOUD)
    add("Echo zu laut für die Referenz: Audioausgang leiser");
  if (r.warnings & aec_self_test::WARN_REVERB_LONG)
    add("Nachhall länger als das Echofilter");
  if (r.warnings & aec_self_test::WARN_LOW_SNR)
    add("Raum zu laut für einige Bänder");
  if (r.warnings & aec_self_test::WARN_NOT_CONVERGED)
    add("Echounterdrückung nicht eingeschwungen");
  if (r.warnings & aec_self_test::WARN_LOW_ERLE)
    add("Echodämpfung unter 10 dB");
  if (r.warnings & aec_self_test::WARN_REF_CLIPPED)
    add("Referenz übersteuert, REF_GAIN bleibt");
  if (r.warnings & aec_self_test::WARN_UNSETTLED)
    add("Pegel am Ende nicht stabil: Messung wiederholen");
  return fit(text.empty() ? "Keine" : text);
}

}  // namespace aivi_aec
