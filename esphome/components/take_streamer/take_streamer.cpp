#include "take_streamer.h"

#ifdef USE_ESP32

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include <sys/select.h>

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <cinttypes>
#include <cstdio>
#include <cstdlib>

namespace esphome::take_streamer {

static const char *const TAG = "take_streamer";

static constexpr uint32_t SAMPLE_RATE = 16000;
static constexpr uint32_t BYTES_PER_SAMPLE = 2;
static constexpr uint32_t BYTES_PER_MS = SAMPLE_RATE * BYTES_PER_SAMPLE / 1000;
static constexpr size_t CHUNK_BYTES = 2048;  // 64 ms per audio-chunk
static constexpr size_t MAX_BYTES_PER_LOOP = 8192;
static constexpr size_t MAX_ANSWER_BYTES = 256;
static constexpr float MAX_SECONDS = 7200.0f;
static constexpr size_t MAX_SPEAKER = 24;
static constexpr size_t MAX_DETECTION_NAME = 32;
static constexpr size_t MAX_PENDING_DETECTIONS = 32;
static constexpr uint32_t CONNECT_TIMEOUT_MS = 3000;
static constexpr uint32_t ACCEPT_TIMEOUT_MS = 3000;
static constexpr uint32_t SAVED_TIMEOUT_MS = 5000;
static const char *const AUDIO_FORMAT = "{\"rate\":16000,\"width\":2,\"channels\":1}";

/// The value of "key" in an answer line: a string without quotes, or the
/// text of a number. The recorder writes flat ASCII answers whose values
/// never contain quotes, commas or braces.
static std::string answer_field(const std::string &line, const char *key) {
  const std::string pattern = std::string("\"") + key + "\":";
  size_t start = line.find(pattern);
  if (start == std::string::npos)
    return "";
  start += pattern.size();
  if (start < line.size() && line[start] == '"') {
    const size_t end = line.find('"', start + 1);
    return end == std::string::npos ? "" : line.substr(start + 1, end - start - 1);
  }
  const size_t end = line.find_first_of(",}", start);
  return line.substr(start, end == std::string::npos ? std::string::npos : end - start);
}

/// Lower case letters and digits, with single "_" or "-" in between, as the
/// recorder requires. Such a name never contains the "__" that separates
/// the parts of an imported clip name.
static bool valid_speaker(const std::string &speaker) {
  if (speaker.empty() || speaker.size() > MAX_SPEAKER)
    return false;
  bool after_separator = true;  // no separator at the start
  for (char c : speaker) {
    const bool separator = c == '_' || c == '-';
    if (separator) {
      if (after_separator)
        return false;
    } else if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9'))) {
      return false;
    }
    after_separator = separator;
  }
  return !after_separator;
}

/// A wake word name ("Hey AIVI") with only characters the recorder accepts.
static std::string clean_name(const std::string &name) {
  std::string result;
  for (char c : name) {
    if (result.size() == MAX_DETECTION_NAME)
      break;
    const bool keep = std::isalnum(static_cast<unsigned char>(c)) || c == ' ' || c == '_' || c == '.' || c == '-';
    result += keep ? c : '_';
  }
  return result.empty() ? "unknown" : result;
}

void TakeStreamer::setup() {
  const size_t buffer_bytes = this->buffer_ms_ * BYTES_PER_MS;
  this->ring_buffer_ = ring_buffer::RingBuffer::create(buffer_bytes);
  if (this->ring_buffer_ == nullptr) {
    ESP_LOGE(TAG, "Could not allocate the %u byte audio buffer", static_cast<unsigned>(buffer_bytes));
    this->mark_failed();
    return;
  }
  this->chunk_.resize(CHUNK_BYTES);

  this->microphone_source_->add_data_callback([this](const std::vector<uint8_t> &data) {
    // Runs in the microphone task: copy only, never block. After an
    // overflow nothing more is written, so the take has no gap.
    if (!this->capturing_.load(std::memory_order_acquire) || this->overflow_.load(std::memory_order_relaxed))
      return;
    const uint32_t captured = this->captured_bytes_.load(std::memory_order_relaxed);
    if (captured >= this->budget_bytes_)
      return;
    const size_t length = std::min<size_t>(data.size(), this->budget_bytes_ - captured);
    if (this->ring_buffer_->write_without_replacement(data.data(), length, 0, false) == length) {
      this->captured_bytes_.store(captured + length, std::memory_order_release);
    } else {
      this->dropped_bytes_.fetch_add(length, std::memory_order_relaxed);
      this->overflow_.store(true, std::memory_order_release);
    }
  });

#ifdef USE_OTA_STATE_LISTENER
  ota::get_global_ota_callback()->add_global_state_listener(this);
#endif
  this->disable_loop();
}

void TakeStreamer::dump_config() {
  ESP_LOGCONFIG(TAG,
                "Take Streamer:\n"
                "  Recorder: %s:%u\n"
                "  Satellite: %s\n"
                "  Buffer: %" PRIu32 " ms",
                this->host_.c_str(), this->port_, this->satellite_id_.c_str(), this->buffer_ms_);
}

#ifdef USE_OTA_STATE_LISTENER
void TakeStreamer::on_ota_global_state(ota::OTAState state, float progress, uint8_t error, ota::OTAComponent *comp) {
  // The upload blocks the loop; end the take now. The recorder keeps the
  // audio it has received.
  if (state == ota::OTA_STARTED && this->is_active())
    this->fail_("ota");
}
#endif

bool TakeStreamer::start_take(const std::string &kind, const std::string &speaker, float seconds) {
  if (this->is_active()) {
    ESP_LOGW(TAG, "A take is already running");
    return false;
  }
  this->kind_ = kind;
  this->speaker_.clear();
  if (kind == "positive") {
    for (char c : speaker)
      this->speaker_ += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  }
  this->requested_seconds_ = seconds;
  this->take_name_.clear();
  this->end_reason_.clear();
  this->error_code_.clear();
  this->stop_reason_.clear();
  this->saved_seconds_ = 0.0f;
  this->detections_ = 0;

  if (this->is_failed()) {
    this->fail_("no_memory");
    return false;
  }
  if (kind != "positive" && kind != "negative") {
    this->fail_("bad_request");
    return false;
  }
  if (kind == "positive" && !valid_speaker(this->speaker_)) {
    this->fail_("bad_speaker");
    return false;
  }
  if (!(seconds > 0.0f && seconds <= MAX_SECONDS)) {
    this->fail_("bad_duration");
    return false;
  }

  this->socket_ = socket::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (this->socket_ == nullptr) {
    this->fail_("no_socket");
    return false;
  }
  this->socket_->setblocking(false);
  int enable = 1;
  this->socket_->setsockopt(IPPROTO_TCP, TCP_NODELAY, &enable, sizeof(enable));
  struct sockaddr_storage address;
  const socklen_t length =
      socket::set_sockaddr(reinterpret_cast<struct sockaddr *>(&address), sizeof(address), this->host_, this->port_);
  if (length == 0 ||
      (this->socket_->connect(reinterpret_cast<struct sockaddr *>(&address), length) != 0 && errno != EINPROGRESS)) {
    this->fail_("unreachable");
    return false;
  }
  this->peer_closed_ = false;
  this->set_state_(State::CONNECTING);
  this->enable_loop();
  this->report_("connecting");
  return true;
}

void TakeStreamer::stop_take(const std::string &reason) {
  switch (this->state_) {
    case State::IDLE:
    case State::FINISHING:
      return;
    case State::CONNECTING:
    case State::WAITING_ACCEPT:
      this->fail_("cancelled");
      return;
    case State::STREAMING:
      if (this->stop_reason_.empty()) {
        this->stop_reason_ = reason.empty() ? "stopped" : reason;
        this->stop_capture_();
      }
      return;
  }
}

void TakeStreamer::mark_detection(const std::string &name) {
  if (this->state_ != State::STREAMING || !this->capturing_.load(std::memory_order_acquire) ||
      this->pending_detections_.size() >= MAX_PENDING_DETECTIONS)
    return;
  // The position in the take, from the samples captured so far.
  const uint32_t position_ms = this->captured_bytes_.load(std::memory_order_acquire) / BYTES_PER_MS;
  this->pending_detections_.emplace_back(clean_name(name), position_ms);
  this->detections_++;
}

void TakeStreamer::loop() {
  switch (this->state_) {
    case State::IDLE:
      this->disable_loop();
      break;
    case State::CONNECTING:
      this->loop_connecting_();
      break;
    case State::WAITING_ACCEPT:
      this->loop_waiting_accept_();
      break;
    case State::STREAMING:
      this->loop_streaming_();
      break;
    case State::FINISHING:
      this->loop_finishing_();
      break;
  }
}

void TakeStreamer::set_state_(State state) {
  this->state_ = state;
  this->state_since_ms_ = millis();
}

void TakeStreamer::loop_connecting_() {
  const int fd = this->socket_->get_fd();
  fd_set writable;
  FD_ZERO(&writable);
  FD_SET(fd, &writable);
  struct timeval no_wait = {0, 0};
  const int ready = ::select(fd + 1, nullptr, &writable, nullptr, &no_wait);
  if (ready > 0) {
    int error = 0;
    socklen_t length = sizeof(error);
    if (this->socket_->getsockopt(SOL_SOCKET, SO_ERROR, &error, &length) != 0 || error != 0) {
      this->fail_("unreachable");
      return;
    }
    char data[384];
    const int size = snprintf(data, sizeof(data),
                              "{\"satellite\":\"%s\",\"kind\":\"%s\",\"speaker\":\"%s\",\"seconds\":%.1f,"
                              "\"token\":\"%s\",\"rate\":16000,\"width\":2,\"channels\":1,\"version\":1}",
                              this->satellite_id_.c_str(), this->kind_.c_str(), this->speaker_.c_str(),
                              this->requested_seconds_, this->token_.c_str());
    if (size <= 0 || static_cast<size_t>(size) >= sizeof(data)) {
      this->fail_("bad_request");
      return;
    }
    this->queue_event_("take-start", data);
    this->set_state_(State::WAITING_ACCEPT);
    return;
  }
  if (ready < 0 || millis() - this->state_since_ms_ > CONNECT_TIMEOUT_MS)
    this->fail_("unreachable");
}

void TakeStreamer::loop_waiting_accept_() {
  if (!this->flush_output_()) {
    this->fail_("connection_lost");
    return;
  }
  std::string line;
  const int got = this->read_answer_(line);
  if (got < 0) {
    this->fail_("connection_lost");
    return;
  }
  if (got == 0) {
    if (millis() - this->state_since_ms_ > ACCEPT_TIMEOUT_MS)
      this->fail_("no_answer");
    return;
  }
  const std::string type = answer_field(line, "type");
  if (type == "error") {
    const std::string code = answer_field(line, "code");
    this->fail_(code.empty() ? "refused" : code.c_str());
    return;
  }
  if (type != "take-accepted") {
    this->fail_("protocol");
    return;
  }

  // The take starts now, so the file begins at a defined moment.
  this->take_name_ = answer_field(line, "name");
  this->ring_buffer_->reset();
  this->captured_bytes_.store(0, std::memory_order_relaxed);
  this->dropped_bytes_.store(0, std::memory_order_relaxed);
  this->overflow_.store(false, std::memory_order_relaxed);
  this->budget_bytes_ = static_cast<uint32_t>(this->requested_seconds_ * SAMPLE_RATE) * BYTES_PER_SAMPLE;
  this->capturing_.store(true, std::memory_order_release);
  this->microphone_source_->start();
  this->source_started_ = true;
  this->set_state_(State::STREAMING);
  ESP_LOGI(TAG, "Take %s started (%s, %.1f s)", this->take_name_.c_str(), this->kind_.c_str(),
           this->requested_seconds_);
  this->report_("recording");
}

void TakeStreamer::loop_streaming_() {
  std::string line;
  const int got = this->read_answer_(line);
  if (got < 0) {
    this->fail_("connection_lost");
    return;
  }
  if (got > 0) {
    // The recorder ended the take itself, e.g. because the drive is full.
    if (!this->handle_final_answer_(line))
      this->fail_("protocol");
    return;
  }
  if (this->overflow_.load(std::memory_order_acquire) && this->stop_reason_.empty()) {
    ESP_LOGW(TAG, "The network was too slow; ending the take");
    this->stop_reason_ = "overflow";
    this->stop_capture_();
  }

  size_t moved = 0;
  while (moved < MAX_BYTES_PER_LOOP) {
    if (!this->flush_output_()) {
      this->fail_("connection_lost");
      return;
    }
    if (!this->output_.empty())
      break;  // The socket is full; go on in the next loop.
    if (!this->pending_detections_.empty()) {
      const auto &detection = this->pending_detections_.front();
      char data[96];
      snprintf(data, sizeof(data), "{\"name\":\"%s\",\"timestamp\":%" PRIu32 "}", detection.first.c_str(),
               detection.second);
      this->pending_detections_.erase(this->pending_detections_.begin());
      this->queue_event_("detection", data);
      continue;
    }
    const size_t length = this->ring_buffer_->read(this->chunk_.data(), this->chunk_.size(), 0);
    if (length == 0)
      break;
    this->queue_event_("audio-chunk", AUDIO_FORMAT, this->chunk_.data(), length);
    moved += length;
  }

  // Every end reason first sends all captured audio, then audio-stop.
  const bool complete = this->captured_bytes_.load(std::memory_order_acquire) >= this->budget_bytes_;
  if ((complete || !this->stop_reason_.empty()) && this->ring_buffer_->available() == 0 && this->output_.empty() &&
      this->pending_detections_.empty()) {
    this->begin_finishing_();
  }
}

void TakeStreamer::begin_finishing_() {
  this->stop_capture_();
  this->end_reason_ = this->stop_reason_.empty() ? "completed" : this->stop_reason_;
  char data[96];
  snprintf(data, sizeof(data), "{\"reason\":\"%s\",\"dropped_bytes\":%" PRIu32 "}", this->end_reason_.c_str(),
           this->dropped_bytes_.load(std::memory_order_relaxed));
  this->queue_event_("audio-stop", data);
  this->set_state_(State::FINISHING);
}

void TakeStreamer::loop_finishing_() {
  if (!this->flush_output_()) {
    this->fail_("connection_lost");
    return;
  }
  std::string line;
  const int got = this->read_answer_(line);
  if (got < 0) {
    this->fail_("connection_lost");
    return;
  }
  if (got == 0) {
    if (millis() - this->state_since_ms_ > SAVED_TIMEOUT_MS)
      this->fail_("no_answer");
    return;
  }
  if (!this->handle_final_answer_(line))
    this->fail_("protocol");
}

bool TakeStreamer::handle_final_answer_(const std::string &line) {
  const std::string type = answer_field(line, "type");
  if (type == "take-saved") {
    const std::string name = answer_field(line, "name");
    if (!name.empty())
      this->take_name_ = name;
    this->saved_seconds_ = strtof(answer_field(line, "seconds").c_str(), nullptr);
    const std::string end = answer_field(line, "end");
    if (!end.empty())
      this->end_reason_ = end;
    ESP_LOGI(TAG, "Take %s saved: %.1f s, end %s, %" PRIu32 " detections", this->take_name_.c_str(),
             this->saved_seconds_, this->end_reason_.c_str(), this->detections_);
    this->close_();
    this->report_("saved");
    return true;
  }
  if (type == "error") {
    const std::string code = answer_field(line, "code");
    this->fail_(code.empty() ? "refused" : code.c_str());
    return true;
  }
  return false;
}

void TakeStreamer::stop_capture_() {
  this->capturing_.store(false, std::memory_order_release);
  if (this->source_started_) {
    this->microphone_source_->stop();
    this->source_started_ = false;
  }
}

void TakeStreamer::queue_event_(const char *type, const char *data, const uint8_t *payload, size_t length) {
  char header[512];
  int size;
  if (length > 0) {
    size = snprintf(header, sizeof(header), "{\"type\":\"%s\",\"data\":%s,\"payload_length\":%u}\n", type, data,
                    static_cast<unsigned>(length));
  } else {
    size = snprintf(header, sizeof(header), "{\"type\":\"%s\",\"data\":%s}\n", type, data);
  }
  if (size <= 0 || static_cast<size_t>(size) >= sizeof(header)) {
    ESP_LOGE(TAG, "Event %s too long", type);
    return;
  }
  this->output_.insert(this->output_.end(), header, header + size);
  if (length > 0)
    this->output_.insert(this->output_.end(), payload, payload + length);
}

bool TakeStreamer::flush_output_() {
  while (this->output_offset_ < this->output_.size()) {
    const ssize_t written =
        this->socket_->write(this->output_.data() + this->output_offset_, this->output_.size() - this->output_offset_);
    if (written > 0) {
      this->output_offset_ += written;
      continue;
    }
    if (written < 0 && (errno == EWOULDBLOCK || errno == EAGAIN))
      return true;
    return false;
  }
  this->output_.clear();
  this->output_offset_ = 0;
  return true;
}

int TakeStreamer::read_answer_(std::string &line) {
  char buffer[128];
  while (!this->peer_closed_) {
    const ssize_t received = this->socket_->read(buffer, sizeof(buffer));
    if (received > 0) {
      this->input_.append(buffer, received);
      continue;
    }
    if (received == 0) {
      this->peer_closed_ = true;
      break;
    }
    if (errno == EWOULDBLOCK || errno == EAGAIN)
      break;
    return -1;
  }
  // An answer the recorder sent just before closing is still read.
  const size_t end = this->input_.find('\n');
  if (end != std::string::npos) {
    line = this->input_.substr(0, end);
    this->input_.erase(0, end + 1);
    return 1;
  }
  if (this->peer_closed_ || this->input_.size() > MAX_ANSWER_BYTES)
    return -1;
  return 0;
}

void TakeStreamer::fail_(const char *code) {
  this->error_code_ = code;
  ESP_LOGW(TAG, "Take %s ended: %s", this->take_name_.empty() ? "(not started)" : this->take_name_.c_str(), code);
  this->close_();
  this->report_("error");
}

void TakeStreamer::close_() {
  this->stop_capture_();
  if (this->socket_ != nullptr) {
    this->socket_->close();
    this->socket_.reset();
  }
  this->output_.clear();
  this->output_offset_ = 0;
  this->input_.clear();
  this->pending_detections_.clear();
  this->set_state_(State::IDLE);
}

void TakeStreamer::report_(const char *status) { this->status_trigger_.trigger(std::string(status)); }

}  // namespace esphome::take_streamer

#endif  // USE_ESP32
