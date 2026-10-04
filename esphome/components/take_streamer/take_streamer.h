#pragma once

#ifdef USE_ESP32

#include "esphome/components/microphone/microphone_source.h"
#include "esphome/components/ring_buffer/ring_buffer.h"
#include "esphome/components/socket/socket.h"

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/core/defines.h"

#ifdef USE_OTA_STATE_LISTENER
#include "esphome/components/ota/ota_backend.h"
#endif

#include <atomic>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace esphome::take_streamer {

/// Streams wake word training takes to the Jetson recorder
/// (services/wake_word_recorder) over TCP, framed as Wyoming events:
///
///   take-start   ->            the request, with the shared token
///                <- take-accepted {name} | error {code}
///   audio-chunk  ->            16 kHz mono 16-bit PCM, the microWakeWord input
///   detection    ->            wake word detections, ms from the take start
///   audio-stop   ->            {reason, dropped_bytes}
///                <- take-saved {name, seconds, end} | error {code}
///
/// The microphone callback only copies audio into a PSRAM ring buffer; all
/// network I/O happens in loop() on a non-blocking socket. Durations and
/// detection times count samples, never wall time. If the network stalls
/// longer than the buffer, the take ends with what was recorded, so a file
/// never has a silent gap.
class TakeStreamer final : public Component
#ifdef USE_OTA_STATE_LISTENER
    ,
                           public ota::OTAGlobalStateListener
#endif
{
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_CONNECTION; }

#ifdef USE_OTA_STATE_LISTENER
  void on_ota_global_state(ota::OTAState state, float progress, uint8_t error, ota::OTAComponent *comp) override;
#endif

  void set_microphone_source(microphone::MicrophoneSource *source) { this->microphone_source_ = source; }
  void set_host(const std::string &host) { this->host_ = host; }
  void set_port(uint16_t port) { this->port_ = port; }
  void set_token(const std::string &token) { this->token_ = token; }
  void set_satellite_id(const std::string &satellite_id) { this->satellite_id_ = satellite_id; }
  void set_buffer_duration(uint32_t buffer_ms) { this->buffer_ms_ = buffer_ms; }

  /// Starts a take. kind is "positive" (needs a speaker: lower case letters,
  /// digits and single "_" or "-" in between) or "negative". Returns false
  /// and reports the status "error" if the take cannot start.
  bool start_take(const std::string &kind, const std::string &speaker, float seconds);
  /// Ends a running take; the audio recorded so far is kept. Before the
  /// recorder has accepted the take, it is cancelled instead.
  void stop_take(const std::string &reason);
  /// Notes a wake word detection at the current position of the take.
  void mark_detection(const std::string &name);

  /// From start_take() until the take has ended.
  bool is_active() const { return this->state_ != State::IDLE; }
  /// From take-accepted until the take has ended.
  bool is_recording() const { return this->state_ == State::STREAMING || this->state_ == State::FINISHING; }
  bool is_positive() const { return this->kind_ == "positive"; }

  // Details for the status text.
  const std::string &kind() const { return this->kind_; }
  const std::string &speaker() const { return this->speaker_; }
  float requested_seconds() const { return this->requested_seconds_; }
  const std::string &take_name() const { return this->take_name_; }
  float saved_seconds() const { return this->saved_seconds_; }
  const std::string &end_reason() const { return this->end_reason_; }
  const std::string &error_code() const { return this->error_code_; }
  uint32_t detections() const { return this->detections_; }

  /// Fires with "connecting", "recording", "saved" or "error".
  Trigger<std::string> *get_status_trigger() { return &this->status_trigger_; }

 protected:
  enum class State : uint8_t {
    IDLE,
    CONNECTING,      // TCP connect in progress
    WAITING_ACCEPT,  // take-start sent, waiting for the answer
    STREAMING,       // audio flows
    FINISHING,       // audio-stop sent, waiting for take-saved
  };

  void set_state_(State state);
  void loop_connecting_();
  void loop_waiting_accept_();
  void loop_streaming_();
  void loop_finishing_();
  void begin_finishing_();
  void stop_capture_();

  /// Queues one event; the payload follows the header line.
  void queue_event_(const char *type, const char *data, const uint8_t *payload = nullptr, size_t length = 0);
  /// Writes queued bytes; false on a socket error.
  bool flush_output_();
  /// 1 with a complete answer line in `line`, 0 if none yet, -1 on errors.
  int read_answer_(std::string &line);
  /// Handles take-saved or error; false for anything else.
  bool handle_final_answer_(const std::string &line);

  void fail_(const char *code);
  void close_();
  void report_(const char *status);

  microphone::MicrophoneSource *microphone_source_{nullptr};
  std::unique_ptr<ring_buffer::RingBuffer> ring_buffer_;
  std::unique_ptr<socket::Socket> socket_;

  std::string host_;
  uint16_t port_{10800};
  std::string token_;
  std::string satellite_id_;
  uint32_t buffer_ms_{5000};

  State state_{State::IDLE};
  uint32_t state_since_ms_{0};

  std::string kind_;
  std::string speaker_;
  float requested_seconds_{0.0f};
  std::string take_name_;
  float saved_seconds_{0.0f};
  std::string end_reason_;
  std::string error_code_;
  std::string stop_reason_;
  uint32_t detections_{0};
  bool source_started_{false};
  bool peer_closed_{false};

  // Shared with the microphone task. budget_bytes_ is set before capturing_
  // turns true and not changed while it is.
  std::atomic<bool> capturing_{false};
  std::atomic<bool> overflow_{false};
  std::atomic<uint32_t> captured_bytes_{0};
  std::atomic<uint32_t> dropped_bytes_{0};
  uint32_t budget_bytes_{0};

  std::vector<uint8_t> output_;
  size_t output_offset_{0};
  std::string input_;
  std::vector<std::pair<std::string, uint32_t>> pending_detections_;
  std::vector<uint8_t> chunk_;

  Trigger<std::string> status_trigger_;
};

}  // namespace esphome::take_streamer

#endif  // USE_ESP32
