#include "sim.h"

#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <math.h>
#include <map>
#include <set>
#include <algorithm>
#include <fstream>
#include <sstream>

namespace sim {

// ===========================================================================
// Board profiles
//
// Two boards, because the interesting question is not "does this sketch work"
// -- it demonstrably works on the car -- but "what does this sketch do when the
// pins underneath it change". The autosport map is README section 6 verbatim;
// the divider ratios are section 3.

static const double R_BYPASS = 1.0000;   // 0-3.3 V, 10k shorted
static const double R_5V     = 0.5769;   // 0-5 V,  15/26 (s3zero only now)
static const double R_16V    = 0.1673;   // 0-16 V, 2.21/13.21 -- the only
                                         // ratio on the carrier board

static Board make_s3zero() {
  Board b;
  b.name = "s3zero";
  const PinDef pins[] = {
      // The R53 shift-light board: a Waveshare ESP32-S3-Zero, an SN65HVD230,
      // an 8-LED strip and a 10k/10k divider on the wideband.
      {1, ROLE_ANALOG_IN,  "WIDEBAND", 0.5, RAIL_NONE},
      {4, ROLE_WS2812_DIN, "LED_DIN",  0.0, RAIL_NONE},
      {5, ROLE_CAN_TX,     "CAN_TX",   0.0, RAIL_NONE},
      {6, ROLE_CAN_RX,     "CAN_RX",   0.0, RAIL_NONE},
      {7, ROLE_I2C_SDA,    "SDA",      0.0, RAIL_NONE},
      {8, ROLE_I2C_SCL,    "SCL",      0.0, RAIL_NONE},
  };
  b.pins.assign(pins, pins + sizeof(pins) / sizeof(pins[0]));
  b.adc_fullscale = 3.10;
  b.adc_gain_err = 0.015;    // internal ADC, +-1-2% even calibrated
  b.adc_bits = 12;
  b.ws2812_buffered = false;
  b.has_pwr_fail = false;
  b.pwr_fail_trip = 0.0;
  b.pwr_fail_hyst = 0.0;
  b.supply_floor = 0.0;
  b.ridethru_shed_ms = 20.0;   // a dev board's decoupling, nothing more
  b.ridethru_noshed_ms = 20.0;
  return b;
}

static Board make_autosport() {
  Board b;
  b.name = "autosport";
  const PinDef pins[] = {
      {4,  ROLE_ANALOG_IN,   "AIN1",       R_16V, RAIL_5VS},
      {5,  ROLE_ANALOG_IN,   "AIN2",       R_16V, RAIL_5VS},
      {6,  ROLE_ANALOG_IN,   "AIN3",       R_16V, RAIL_5VS},
      {7,  ROLE_ANALOG_IN,   "AIN4",       R_16V, RAIL_5VS},
      // 8.2k / 108.2k, not the 1/11 this model used to carry. The schematic
      // moved to 13.2:1 so a 36 V input would not saturate the ADC, and the
      // model did not follow -- study 0 compares NETS, not ratios, so it
      // could not see the drift.
      {8,  ROLE_VBAT_SNS,    "VBAT_SNS",   8.2 / 108.2, RAIL_NONE},
      {47, ROLE_SD_PWR_EN,   "SD_PWR_EN",  0.0, RAIL_NONE},
      {11, ROLE_SD_CD,       "SD_CD",      0.0, RAIL_NONE},
      // 1-bit SDMMC: D1/D2/D3 do not reach the MCU. Their three pins are the
      // SPI bus below.
      {12, ROLE_SD_BUS,      "SD_D0",      0.0, RAIL_NONE},
      {13, ROLE_SD_BUS,      "SD_CMD",     0.0, RAIL_NONE},
      {14, ROLE_SD_BUS,      "SD_CLK",     0.0, RAIL_NONE},
      {15, ROLE_PWR_FAIL,    "PWR_FAIL",   0.0, RAIL_NONE},
      {17, ROLE_CAN_TX,      "CAN_TX",     0.0, RAIL_NONE},
      {18, ROLE_CAN_RX,      "CAN_RX",     0.0, RAIL_NONE},
      {16, ROLE_CAN_S,       "CAN_S",      0.0, RAIL_NONE},
      // Net names, not header positions -- gen/simulate_firmware.py study 0
      // diffs this table against netlist.txt and needs the same vocabulary.
      {35, ROLE_PSRAM,       "PSRAM",      0.0, RAIL_NONE},
      {36, ROLE_PSRAM,       "PSRAM",      0.0, RAIL_NONE},
      {37, ROLE_PSRAM,       "PSRAM",      0.0, RAIL_NONE},
      // IO38 drives the DevKitC-1 v1.1 onboard RGB LED, so SDA moved to IO47.
      {10, ROLE_I2C_SDA,     "I2C_SDA",    0.0, RAIL_NONE},
      {9,  ROLE_I2C_SCL,     "I2C_SCL",    0.0, RAIL_NONE},
      {48, ROLE_WS2812_DIN,  "LED_DIN_MCU", 0.0, RAIL_NONE},
      // K-line. The pins are modelled so study 0 holds them against the
      // netlist and the sketch can drive them; there is no KWP2000 protocol
      // model behind them yet.
      {2,  ROLE_KLINE_TX,    "K_TX",       0.0, RAIL_NONE},
      {1,  ROLE_KLINE_RX,    "K_RX",       0.0, RAIL_NONE},
      // Second CAN bus. An MCP2518FD on SPI, not a second TWAI -- the S3
      // only has one. Modelled as pins so study 0 holds them against the
      // netlist; there is no controller model behind them.
      {39, ROLE_CAN2_SPI,    "CAN2_SCK",   0.0, RAIL_NONE},
      {40, ROLE_CAN2_SPI,    "CAN2_MOSI",  0.0, RAIL_NONE},
      {41, ROLE_CAN2_SPI,    "CAN2_MISO",  0.0, RAIL_NONE},
      {42, ROLE_CAN2_SPI,    "CAN2_CS",    0.0, RAIL_NONE},
      {21, ROLE_CAN2_INT,    "CAN2_INT",   0.0, RAIL_NONE},
  };
  b.pins.assign(pins, pins + sizeof(pins) / sizeof(pins[0]));
  b.adc_fullscale = 3.10;
  b.adc_gain_err = 0.015;
  b.adc_bits = 12;
  b.ws2812_buffered = true;      // 74AHCT1G125, input floats until driven
  b.has_pwr_fail = true;
  // The board runs off USB now, so the rail being watched is the dev board's
  // 5 V pin, not a 12 V harness. Trip is the TLV431 divider: 1.24 * 40.7/12.0.
  b.pwr_fail_trip = 4.20;
  b.pwr_fail_hyst = 0.10;
  b.supply_floor = 3.60;         // DevKitC-1 LDO dropout at ~120 mA
  // Hold-up is a 0.5 F supercapacitor bank (2 x 1 F 2.7 V in series) on the
  // 5 V rail, discharging through a Schottky into the dev board.
  //
  //   t = C * dV / I,  dV = 4.2 (trip) - 3.6 (LDO dropout) = 0.6 V
  //     shed   120 mA (radio down, LEDs dark)  -> 0.5 * 0.6 / 0.120 = 2500 ms
  //     noshed 390 mA (radio, LEDs, sensors)   -> 0.5 * 0.6 / 0.390 =  769 ms
  //
  // TODO these two are hand-calculated, unlike the 154.4/74.7 they replace,
  // which came out of gen/simulate.py study 4. The equivalent ngspice study
  // for the supercap bank has not been written yet, so treat them as an
  // estimate rather than a measurement -- an ESR term and the Schottky's
  // forward drop over temperature will both eat into them.
  b.ridethru_shed_ms = 2500.0;
  b.ridethru_noshed_ms = 769.0;
  return b;
}

const char* role_name(Role r) {
  switch (r) {
    case ROLE_ANALOG_IN: return "analog input";
    case ROLE_VBAT_SNS: return "battery monitor";
    case ROLE_CAN_TX: return "CAN TX";
    case ROLE_CAN_RX: return "CAN RX";
    case ROLE_CAN_S: return "CAN silent-mode select";
    case ROLE_WS2812_DIN: return "WS2812 data";
    case ROLE_I2C_SDA: return "I2C SDA";
    case ROLE_I2C_SCL: return "I2C SCL";
    case ROLE_SD_PWR_EN: return "microSD supply enable";
    case ROLE_SD_CD: return "microSD card detect";
    case ROLE_SD_BUS: return "microSD bus";
    case ROLE_PWR_FAIL: return "power-fail interrupt";
    case ROLE_SENS_EN: return "sensor-rail enable";
    case ROLE_KLINE_TX: return "K-line transmit";
    case ROLE_KLINE_RX: return "K-line receive";
    case ROLE_CAN2_SPI: return "second CAN controller, SPI";
    case ROLE_CAN2_INT: return "second CAN controller, interrupt";
    case ROLE_USB: return "native USB";
    case ROLE_UART: return "UART0";
    case ROLE_SPI: return "SPI breakout";
    case ROLE_BOOT: return "BOOT button";
    case ROLE_SPARE_STRAP: return "spare IO (strapping pin)";
    case ROLE_PSRAM: return "octal PSRAM (unusable)";
    default: return "not connected";
  }
}

// ===========================================================================
// State

namespace {

struct Event {
  uint64_t t_us;
  std::vector<std::string> argv;
  size_t seq;
};

struct State {
  const Board* board;
  uint64_t t_us;

  // faults
  std::vector<std::string> log;
  std::set<std::string> seen;
  int counts[3];

  // pins
  std::map<int, int> level;       // firmware-driven output level
  std::map<int, int> mode;
  std::set<int> driven;
  std::map<int, void (*)()> isr;
  std::map<int, int> isr_mode;

  // analog
  double chan_src[9];
  double chan_ratio[9];
  Rail chan_rail[9];
  int adc_bits;
  int adc_last_mv;

  // CAN
  bool can_installed, can_started, can_pins_ok, can_off;
  int can_queue_len;
  uint32_t gen_id;
  double gen_hz, gen_rpm;
  uint64_t gen_next_us;
  std::vector<Frame> rxq;
  unsigned long generated, delivered, dropped;

  // LEDs
  bool leds_ok;
  int led_gpio, led_count;
  std::vector<Rgb> led_last;
  int led_lit;
  Rgb led_first;
  unsigned long led_frames;
  uint8_t led_brightness;

  // I2C / ADS1115
  bool i2c_begun, i2c_pins_ok, ads_present;
  uint16_t ads_config;

  // BLE
  bool ble_init_ok, ble_up, ble_adv, ble_conn, ble_sub;
  unsigned long notifies;
  float last_notify;
  BleHooks hooks;

  // power
  double vbat;     // OBD-II pin 16, permanent battery. Sense only.
  double v5;       // the dev board's 5 V pin: what actually powers the board
  bool pwr_fail;
  bool pwr_fail_observed;
  double pwr_fail_at_ms;
  double reserve;
  bool lost;
  // Scenario override for the ride-through window: study 4 measures the
  // healthy-battery case, but a flat battery is a different and much shorter
  // event, and firmware has to fit inside that one.
  double budget_shed_ms;
  double budget_noshed_ms;

  // SD
  bool sd_begun;
  bool sd_open_flag;
  size_t sd_unflushed_bytes;
  double sd_flush_ms;
  bool sd_stalled;
  bool sd_powered_note;

  // serial
  std::string serial;
  std::string serial_partial;

  // scenario
  std::vector<Event> events;
  size_t ev_next;
  double duration_ms, trace_ms;
  std::string board_request;

  // run control
  bool stop;
  std::string stop_reason;

  // trace
  FILE* trace;
  uint64_t trace_next_us;
  unsigned long heap;
};

State& S() {
  static State s;
  static bool init = false;
  if (!init) {
    init = true;
    memset(s.counts, 0, sizeof s.counts);
    s.board = 0;
    s.t_us = 0;
    for (int i = 0; i < 9; i++) { s.chan_src[i] = 0.0; s.chan_ratio[i] = 1.0; s.chan_rail[i] = RAIL_NONE; }
    s.adc_bits = 12;
    s.adc_last_mv = 0;
    s.can_installed = s.can_started = s.can_off = false;
    s.can_pins_ok = true;
    s.can_queue_len = 5;
    s.gen_id = 0x316; s.gen_hz = 0.0; s.gen_rpm = 0.0; s.gen_next_us = 0;
    s.generated = s.delivered = s.dropped = 0;
    s.leds_ok = false; s.led_gpio = -1; s.led_count = 0; s.led_lit = 0;
    s.led_first.r = s.led_first.g = s.led_first.b = 0;
    s.led_frames = 0; s.led_brightness = 255;
    s.i2c_begun = false; s.i2c_pins_ok = true; s.ads_present = false; s.ads_config = 0;
    s.ble_init_ok = true; s.ble_up = s.ble_adv = s.ble_conn = s.ble_sub = false;
    s.notifies = 0; s.last_notify = 0.0f;
    memset(&s.hooks, 0, sizeof s.hooks);
    s.vbat = 13.8; s.v5 = 4.70; s.pwr_fail = false; s.pwr_fail_observed = false;
    s.pwr_fail_at_ms = -1.0; s.reserve = 1.0; s.lost = false;
    s.budget_shed_ms = 0.0; s.budget_noshed_ms = 0.0;
    s.sd_begun = false;
    s.sd_open_flag = false; s.sd_unflushed_bytes = 0; s.sd_flush_ms = 18.0;
    s.sd_stalled = false; s.sd_powered_note = false;
    s.ev_next = 0; s.duration_ms = 20000.0; s.trace_ms = 20.0;
    s.stop = false;
    s.trace = 0; s.trace_next_us = 0;
    s.heap = 210000;
  }
  return s;
}

const PinDef* find_pin(int gpio) {
  const Board& b = board();
  for (size_t i = 0; i < b.pins.size(); i++)
    if (b.pins[i].gpio == gpio) return &b.pins[i];
  return 0;
}

int channel_of(int gpio) {
  const Board& b = board();
  int n = 0;
  for (size_t i = 0; i < b.pins.size(); i++) {
    if (b.pins[i].role == ROLE_ANALOG_IN) {
      n++;
      if (b.pins[i].gpio == gpio) return n;
    }
  }
  return 0;
}

int gpio_with_role(Role r) {
  const Board& b = board();
  for (size_t i = 0; i < b.pins.size(); i++)
    if (b.pins[i].role == r) return b.pins[i].gpio;
  return -1;
}

bool sens_rail_live() {
  const Board& b = board();
  int en = gpio_with_role(ROLE_SENS_EN);
  if (en < 0) return true;              // no switch on this board; rail is always up
  (void)b;
  return S().level.count(en) && S().level[en] == 1;
}

void trace_emit();

}  // namespace

const Board* board_by_name(const std::string& name) {
  static Board a = make_s3zero();
  static Board c = make_autosport();
  if (name == "s3zero") return &a;
  if (name == "autosport") return &c;
  return 0;
}

const Board& board() {
  if (!S().board) S().board = board_by_name("autosport");
  return *S().board;
}

void set_board(const Board* b) {
  State& s = S();
  s.board = b;
  int n = 0;
  for (size_t i = 0; i < b->pins.size(); i++) {
    if (b->pins[i].role == ROLE_ANALOG_IN) {
      n++;
      if (n < 9) { s.chan_ratio[n] = b->pins[i].ratio; s.chan_rail[n] = b->pins[i].rail; }
    }
  }
}

// ===========================================================================
// Faults

void fault(Severity sev, const char* code, const char* fmt, ...) {
  char detail[512];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(detail, sizeof detail, fmt, ap);
  va_end(ap);

  State& s = S();
  std::string key = std::string(code) + "|" + detail;
  if (s.seen.count(key)) return;
  s.seen.insert(key);

  // Field order is load-bearing: severity, timestamp, code, detail, each
  // whitespace-separated, so gen/simulate_firmware.py can split on it.
  const char* sevname = sev == SEV_ERROR ? "ERROR" : (sev == SEV_WARN ? "WARN " : "note ");
  char line[700];
  snprintf(line, sizeof line, "%s %9.2fms %-20s %s",
           sevname, s.t_us / 1000.0, code, detail);
  s.log.push_back(line);
  s.counts[(int)sev]++;
}

const std::vector<std::string>& faults() { return S().log; }
int fault_count(Severity sev) { return S().counts[(int)sev]; }

// ===========================================================================
// Serial

void serial_out(const char* text) {
  State& s = S();
  for (const char* p = text; *p; p++) {
    if (*p == '\n') {
      char stamp[32];
      snprintf(stamp, sizeof stamp, "[%9.3f] ", s.t_us / 1000.0);
      s.serial += stamp;
      s.serial += s.serial_partial;
      s.serial += '\n';
      s.serial_partial.clear();
    } else if (*p != '\r') {
      s.serial_partial += *p;
    }
  }
}

const std::string& serial_log() { return S().serial; }

// ===========================================================================
// Clock, events and everything that happens as time passes

namespace {

void apply_event(const std::vector<std::string>& a);

// One slice of simulated time, short enough that the power integration and the
// CAN generator do not step over each other.
void step(uint64_t dt_us) {
  State& s = S();
  s.t_us += dt_us;

  // Power: below the TLV431 trip point the board is running off the supercap
  // bank, and how long that lasts depends on what firmware has switched off.
  //
  // The parent could watch a node the bank did not hold up -- it sensed ahead
  // of its ideal diode -- so it saw the harness open before spending any of
  // the ride-through. There is no such node here: USB VBUS is not brought out
  // on the DevKitC-1, so PWR_FAIL and the bank both sit on the 5 V rail and
  // the first part of the window is spent before anything notices. That only
  // became affordable when the budget went from 127 ms to seconds.
  const Board& b = board();
  if (b.has_pwr_fail) {
    bool was = s.pwr_fail;
    if (s.v5 < b.pwr_fail_trip) s.pwr_fail = true;
    else if (s.v5 > b.pwr_fail_trip + b.pwr_fail_hyst) s.pwr_fail = false;
    if (s.pwr_fail && !was) {
      s.pwr_fail_at_ms = s.t_us / 1000.0;
      int pin = gpio_with_role(ROLE_PWR_FAIL);
      std::map<int, void (*)()>::iterator it = s.isr.find(pin);
      if (it != s.isr.end() && it->second) {
        s.pwr_fail_observed = true;
        it->second();
      }
    }
  }
  if (s.v5 < b.pwr_fail_trip) {   // USB is gone; the bank is carrying it
    double shed = s.budget_shed_ms > 0 ? s.budget_shed_ms : b.ridethru_shed_ms;
    double noshed = s.budget_noshed_ms > 0 ? s.budget_noshed_ms : b.ridethru_noshed_ms;
    // What is worth shedding changed with the supply. On the parent it was
    // the sensor rail, 80 mA out of ~350 mA at 12 V, switched by SENS_EN.
    // There is no switch now, and the load that dominates is the shift light:
    // eight WS2812s at full white is 480 mA against ~120 mA for everything
    // else. So the budget turns on whether the strip is dark, which is the
    // first thing shutdown() does.
    double budget_ms = (s.led_lit > 0) ? noshed : shed;
    s.reserve -= (dt_us / 1000.0) / budget_ms;
    if (s.reserve <= 0.0 && !s.lost) {
      s.reserve = 0.0;
      s.lost = true;
      run_stop("rails collapsed (ride-through budget exhausted)");
    }
  } else if (s.reserve < 1.0) {
    s.reserve = std::min(1.0, s.reserve + (dt_us / 1000.0) / 50.0);  // recharge
  }

  // CAN generator.
  if (s.gen_hz > 0.0) {
    uint64_t period = (uint64_t)(1e6 / s.gen_hz);
    if (period == 0) period = 1;
    while (s.gen_next_us <= s.t_us) {
      s.gen_next_us += period;
      s.generated++;
      if (!s.can_started || !s.can_pins_ok || s.can_off) continue;
      Frame f;
      memset(&f, 0, sizeof f);
      f.id = s.gen_id;
      f.dlc = 8;
      // R53 cluster 0x316: RPM in bytes 2-3, little-endian, engineering
      // units are raw/6.4.
      uint16_t raw = (uint16_t)(s.gen_rpm * 6.4 + 0.5);
      f.data[2] = (uint8_t)(raw & 0xFF);
      f.data[3] = (uint8_t)(raw >> 8);
      if ((int)s.rxq.size() >= s.can_queue_len) s.dropped++;
      else s.rxq.push_back(f);
    }
  }

  // Trace.
  while (s.trace && s.t_us >= s.trace_next_us) {
    trace_emit();
    s.trace_next_us += (uint64_t)(s.trace_ms * 1000.0);
  }
}

}  // namespace

uint64_t now_us() { return S().t_us; }

void advance_us(uint64_t us) {
  State& s = S();
  uint64_t target = s.t_us + us;
  while (s.t_us < target) {
    uint64_t next = target;

    if (s.ev_next < s.events.size() && s.events[s.ev_next].t_us < next)
      next = std::max(s.events[s.ev_next].t_us, s.t_us);
    if (s.gen_hz > 0.0 && s.gen_next_us > s.t_us && s.gen_next_us < next)
      next = s.gen_next_us;
    if (s.trace && s.trace_next_us > s.t_us && s.trace_next_us < next)
      next = s.trace_next_us;

    // Cap the slice so the power integration stays accurate and a long delay()
    // cannot skip past a collapse.
    if (next > s.t_us + 1000) next = s.t_us + 1000;

    step(next - s.t_us);

    while (s.ev_next < s.events.size() && s.events[s.ev_next].t_us <= s.t_us) {
      apply_event(s.events[s.ev_next].argv);
      s.ev_next++;
    }
    if (s.stop) return;
  }
}

void charge_us(uint64_t us) { advance_us(us); }

// ===========================================================================
// Pins

void pin_mode(int gpio, int mode) {
  State& s = S();
  s.mode[gpio] = mode;
  const PinDef* p = find_pin(gpio);
  if (!p) {
    fault(SEV_WARN, "PIN_UNKNOWN", "GPIO%d configured, but this board leaves it unconnected", gpio);
    return;
  }
  if (p->role == ROLE_PSRAM)
    fault(SEV_ERROR, "PIN_PSRAM", "GPIO%d is octal PSRAM on this module and cannot be used as IO", gpio);
}

void pin_write(int gpio, int level) {
  State& s = S();
  s.level[gpio] = level;
  s.driven.insert(gpio);
  const PinDef* p = find_pin(gpio);
  if (p && p->role == ROLE_PSRAM)
    fault(SEV_ERROR, "PIN_PSRAM", "GPIO%d driven, but it is octal PSRAM on this module", gpio);
  if (p && p->role == ROLE_SD_PWR_EN && level == 0 && s.sd_begun)
    fault(SEV_WARN, "SD_BACKFEED",
          "SD_PWR_EN (GPIO%d) dropped while the card is still mounted. Call SD_MMC.end() first, "
          "or the MCU keeps driving the bus into an unpowered card through its ESD structures "
          "(README section 5)",
          gpio);
}

int pin_read(int gpio) {
  State& s = S();
  const PinDef* p = find_pin(gpio);
  if (p && p->role == ROLE_PWR_FAIL) {
    s.pwr_fail_observed = true;
    return s.pwr_fail ? 1 : 0;
  }
  if (p && p->role == ROLE_SD_CD) return 1;   // card seated
  if (s.level.count(gpio)) return s.level[gpio];
  return 0;
}

bool pin_driven(int gpio) { return S().driven.count(gpio) != 0; }

void attach_isr(int gpio, void (*fn)(), int mode) {
  State& s = S();
  s.isr[gpio] = fn;
  s.isr_mode[gpio] = mode;
  const PinDef* p = find_pin(gpio);
  if (p && p->role == ROLE_PWR_FAIL) s.pwr_fail_observed = true;
}

// ===========================================================================
// ADC

void adc_set_resolution(int bits) { S().adc_bits = bits; }
void adc_set_attenuation(int, int) {}

static double adc_pin_volts(int gpio) {
  State& s = S();
  const PinDef* p = find_pin(gpio);
  if (!p) {
    fault(SEV_ERROR, "ADC_PIN_UNKNOWN",
          "analog read on GPIO%d, which this board does not connect to anything", gpio);
    return 0.0;
  }
  if (p->role == ROLE_VBAT_SNS) return s.vbat * p->ratio;
  if (p->role != ROLE_ANALOG_IN) {
    fault(SEV_ERROR, "ADC_PIN_ROLE",
          "analog read on GPIO%d, but on this board GPIO%d is %s (%s) -- the reading is meaningless "
          "and the pin may be driven",
          gpio, gpio, p->net, role_name(p->role));
    return 0.0;
  }
  int ch = channel_of(gpio);
  double src = s.chan_src[ch];
  if (s.chan_rail[ch] == RAIL_5VS && !sens_rail_live()) {
    int en = gpio_with_role(ROLE_SENS_EN);
    fault(SEV_ERROR, "SENSOR_RAIL_OFF",
          "channel %d (GPIO%d) is excited from +5VS, which is off at reset -- firmware never drove "
          "SENS_EN (GPIO%d) high, so this channel reads 0 V no matter what the sensor does",
          ch, gpio, en);
    src = 0.0;
  }
  return src * s.chan_ratio[ch];
}

int adc_read_mv(int gpio) {
  State& s = S();
  const Board& b = board();
  charge_us(30);                         // one-shot conversion + calibration
  double v = adc_pin_volts(gpio);
  if (v > b.adc_fullscale) {
    const PinDef* p = find_pin(gpio);
    int ch = p ? channel_of(gpio) : 0;
    fault(SEV_ERROR, "ADC_CLIP",
          "channel %d (GPIO%d) reached %.3f V at the pin against a %.2f V full scale -- the reading "
          "saturates and looks like a plausible value, not a fault",
          ch, gpio, v, b.adc_fullscale);
    v = b.adc_fullscale;
  }
  if (v < 0.0) v = 0.0;
  // Quantise the way the chip does, and keep the internal ADC's gain error so a
  // scenario can tell the two converters apart.
  double lsb = b.adc_fullscale / ((1 << s.adc_bits) - 1);
  double q = floor(v / lsb + 0.5) * lsb;
  q *= (1.0 + b.adc_gain_err);
  s.adc_last_mv = (int)(q * 1000.0 + 0.5);
  return s.adc_last_mv;
}

int adc_read_raw(int gpio) {
  const Board& b = board();
  int mv = adc_read_mv(gpio);
  double frac = (mv / 1000.0) / b.adc_fullscale;
  if (frac > 1.0) frac = 1.0;
  return (int)(frac * ((1 << S().adc_bits) - 1));
}

// ===========================================================================
// CAN

void can_install(int tx, int rx, int mode, int rx_queue_len) {
  State& s = S();
  s.can_installed = true;
  s.can_queue_len = rx_queue_len > 0 ? rx_queue_len : 5;
  (void)mode;

  int want_tx = gpio_with_role(ROLE_CAN_TX);
  int want_rx = gpio_with_role(ROLE_CAN_RX);
  s.can_pins_ok = true;
  if (want_tx >= 0 && tx != want_tx) {
    const PinDef* p = find_pin(tx);
    fault(SEV_ERROR, "CAN_TX_PIN",
          "TWAI TX assigned to GPIO%d (%s), but this board wires the transceiver TXD to GPIO%d -- "
          "no frames will ever arrive, and GPIO%d is now driven by the CAN controller",
          tx, p ? role_name(p->role) : "not connected", want_tx, tx);
    s.can_pins_ok = false;
  }
  if (want_rx >= 0 && rx != want_rx) {
    const PinDef* p = find_pin(rx);
    fault(SEV_ERROR, "CAN_RX_PIN",
          "TWAI RX assigned to GPIO%d (%s), but this board wires the transceiver RXD to GPIO%d",
          rx, p ? role_name(p->role) : "not connected", want_rx, rx);
    s.can_pins_ok = false;
  }

  int cs = gpio_with_role(ROLE_CAN_S);
  if (cs >= 0 && !pin_driven(cs))
    fault(SEV_NOTE, "CAN_S_DEFAULT",
          "CAN_S (GPIO%d) left undriven; R54 pulls it low so the transceiver comes up in normal "
          "mode. Drive it high for listen-only if this node must never ACK",
          cs);
}

bool can_start() {
  State& s = S();
  if (!s.can_installed) return false;
  s.can_started = true;
  return true;
}

bool can_receive(Frame* out) {
  State& s = S();
  if (s.rxq.empty()) return false;
  *out = s.rxq.front();
  s.rxq.erase(s.rxq.begin());
  s.delivered++;
  return true;
}

bool can_bus_off() { return S().can_off; }
void can_recover() { S().can_off = false; }
void can_force_bus_off() { S().can_off = true; S().rxq.clear(); }
void can_set_generator(uint32_t id, double hz) {
  State& s = S();
  s.gen_id = id;
  s.gen_hz = hz;
  s.gen_next_us = s.t_us;
}
void can_set_rpm(double rpm) { S().gen_rpm = rpm; }
void can_inject(const Frame& f) {
  State& s = S();
  if (!s.can_started || !s.can_pins_ok || s.can_off) return;
  if ((int)s.rxq.size() >= s.can_queue_len) s.dropped++;
  else s.rxq.push_back(f);
}
unsigned long can_rx_dropped() { return S().dropped; }

// ===========================================================================
// LEDs

void leds_attach(int gpio, void*, int count) {
  State& s = S();
  s.led_gpio = gpio;
  s.led_count = count;
  const Board& b = board();
  int want = gpio_with_role(ROLE_WS2812_DIN);
  s.leds_ok = (want < 0 || gpio == want);
  if (!s.leds_ok) {
    const PinDef* p = find_pin(gpio);
    fault(SEV_ERROR, "LED_PIN",
          "WS2812 data assigned to GPIO%d (%s), but this board routes the strip from GPIO%d",
          gpio, p ? role_name(p->role) : "not connected", want);
  }
  if (b.ws2812_buffered && s.leds_ok && !pin_driven(gpio))
    fault(SEV_WARN, "LED_BOOT_FLOAT",
          "GPIO%d feeds a 74AHCT1G125 through a 33 ohm series resistor with no pull-down, so the "
          "buffer input is undefined from reset until firmware drives it. Drive GPIO%d low before "
          "handing it to the LED driver",
          gpio, gpio);
}

void leds_show(const Rgb* px, int count, uint8_t brightness) {
  State& s = S();
  // A WS2812 frame is 24 bits at 800 kHz per pixel plus the reset gap, and the
  // driver blocks for it. On a 20 ms loop that is real time the CAN queue is
  // filling in.
  charge_us((uint64_t)(30 * count) + 300);
  s.led_frames++;
  s.led_brightness = brightness;
  s.led_last.assign(px, px + count);
  s.led_lit = 0;
  s.led_first.r = s.led_first.g = s.led_first.b = 0;
  // Bits shifted out of the wrong pin do not reach the strip. Reporting the
  // buffer contents here would show a working shift light on a board where it
  // is dark, which is the opposite of useful.
  if (!s.leds_ok) return;
  for (int i = 0; i < count; i++) {
    if (px[i].r || px[i].g || px[i].b) {
      if (s.led_lit == 0) s.led_first = px[i];
      s.led_lit++;
    }
  }
}

int leds_lit() { return S().led_lit; }
Rgb leds_first_lit() { return S().led_first; }
unsigned long leds_frames() { return S().led_frames; }

// ===========================================================================
// I2C and the ADS1115

void i2c_begin(int sda, int scl) {
  State& s = S();
  s.i2c_begun = true;
  int want_sda = gpio_with_role(ROLE_I2C_SDA);
  int want_scl = gpio_with_role(ROLE_I2C_SCL);
  s.i2c_pins_ok = true;
  if (want_sda >= 0 && sda != want_sda) {
    const PinDef* p = find_pin(sda);
    fault(SEV_ERROR, "I2C_SDA_PIN",
          "I2C SDA brought up on GPIO%d, but on this board GPIO%d is %s (%s) and the bus is on "
          "GPIO%d. Driving it toggles that function",
          sda, sda, p ? p->net : "nothing", p ? role_name(p->role) : "not connected", want_sda);
    s.i2c_pins_ok = false;
  }
  if (want_scl >= 0 && scl != want_scl) {
    const PinDef* p = find_pin(scl);
    fault(SEV_ERROR, "I2C_SCL_PIN",
          "I2C SCL brought up on GPIO%d, but on this board GPIO%d is %s (%s) and the bus is on "
          "GPIO%d",
          scl, scl, p ? p->net : "nothing", p ? role_name(p->role) : "not connected", want_scl);
    s.i2c_pins_ok = false;
  }
}

void ads_set_present(bool present) { S().ads_present = present; }

bool i2c_write_reg(uint8_t addr, uint8_t reg, uint16_t value) {
  State& s = S();
  charge_us(90);                       // 3 bytes at 400 kHz plus overhead
  if (!s.i2c_begun || !s.i2c_pins_ok) return false;
  if (addr != 0x48 || !s.ads_present) return false;
  if (reg == 0x01) s.ads_config = value;
  return true;
}

bool i2c_read_reg(uint8_t addr, uint8_t reg, uint16_t* out) {
  State& s = S();
  charge_us(90);
  if (!s.i2c_begun || !s.i2c_pins_ok) return false;
  if (addr != 0x48 || !s.ads_present) return false;
  if (reg == 0x01) { *out = s.ads_config; return true; }
  if (reg == 0x00) {
    // The ADS1115 shares the conditioned node with the internal ADC, so it sees
    // the same post-divider voltage -- with none of the internal ADC's gain
    // error and no 3.1 V ceiling, which is the whole reason it is fitted.
    int gpio = -1;
    const Board& b = board();
    for (size_t i = 0; i < b.pins.size(); i++)
      if (b.pins[i].role == ROLE_ANALOG_IN) { gpio = b.pins[i].gpio; break; }
    double v = gpio >= 0 ? adc_pin_volts(gpio) : 0.0;
    double lsb = 4.096 / 32768.0;
    long raw = (long)(v / lsb + 0.5);
    if (raw > 32767) raw = 32767;
    *out = (uint16_t)(int16_t)raw;
    return true;
  }
  *out = 0;
  return true;
}

// ===========================================================================
// BLE

void ble_set_init_ok(bool ok) { S().ble_init_ok = ok; }

bool ble_init(const char*) {
  State& s = S();
  charge_us(120000);                   // the controller takes real time to come up
  if (!s.ble_init_ok) return false;
  s.ble_up = true;
  s.heap -= 48000;
  return true;
}

void ble_deinit() {
  State& s = S();
  s.ble_up = s.ble_adv = s.ble_conn = s.ble_sub = false;
  s.heap += 48000;
}

bool ble_adv_start() {
  State& s = S();
  if (!s.ble_up) return false;
  s.ble_adv = true;
  return true;
}

bool ble_adv_active() { return S().ble_adv && !S().ble_conn; }
bool ble_connected() { return S().ble_conn; }

void ble_note_notify(const void* data, size_t len) {
  State& s = S();
  charge_us(200);
  s.notifies++;
  if (len == sizeof(float)) memcpy(&s.last_notify, data, sizeof(float));
}

unsigned long ble_notify_count() { return S().notifies; }
float ble_last_notify_f32() { return S().last_notify; }
void ble_set_hooks(const BleHooks& h) { S().hooks = h; }

void ble_event_connect() {
  State& s = S();
  if (!s.ble_up) {
    fault(SEV_WARN, "BLE_CONNECT_DOWN", "scenario connected a phone, but the stack is not up");
    return;
  }
  s.ble_conn = true;
  s.ble_adv = false;
  if (s.hooks.on_connect) s.hooks.on_connect();
}

void ble_event_disconnect() {
  State& s = S();
  s.ble_conn = false;
  s.ble_sub = false;
  if (s.hooks.on_disconnect) s.hooks.on_disconnect();
  // advertiseOnDisconnect() is what restarts advertising in NimBLE 2.x; the
  // sketch relies on it rather than calling start() from the callback.
  if (s.ble_up) s.ble_adv = true;
}

void ble_event_subscribe(uint16_t v) {
  State& s = S();
  if (!s.ble_conn) {
    fault(SEV_WARN, "BLE_SUB_NOCONN", "scenario subscribed with no connection open");
    return;
  }
  s.ble_sub = (v != 0);
  if (s.hooks.on_subscribe) s.hooks.on_subscribe(v);
}

void ble_event_hwmode(uint8_t v) {
  State& s = S();
  if (s.hooks.on_hwmode_write) s.hooks.on_hwmode_write(v);
}

// ===========================================================================
// Power and sensors

void power_set_vbat(double v) { S().vbat = v; }
void power_set_v5(double v) { S().v5 = v; }
void power_set_budget(double shed_ms, double noshed_ms) {
  S().budget_shed_ms = shed_ms;
  S().budget_noshed_ms = noshed_ms;
}
double power_vbat() { return S().vbat; }
double power_v5() { return S().v5; }
bool power_failed() { return S().pwr_fail; }
double power_reserve() { return S().reserve; }
bool power_lost() { return S().lost; }
double power_fail_at_ms() { return S().pwr_fail_at_ms; }

void sensor_set(int ch, double v) { if (ch >= 1 && ch < 9) S().chan_src[ch] = v; }
void sensor_set_rail(int ch, Rail r) { if (ch >= 1 && ch < 9) S().chan_rail[ch] = r; }

// ===========================================================================
// microSD

void sd_set_pins(int clk, int cmd, int d0, int d1, int d2, int d3) {
  const int want[6] = {14, 13, 12, 11, 10, 9};   // CLK, CMD, D0, D1, D2, D3
  const char* label[6] = {"CLK", "CMD", "D0", "D1", "D2", "D3"};
  const int got[6] = {clk, cmd, d0, d1, d2, d3};
  const Board& b = board();
  bool have_slot = false;
  for (size_t i = 0; i < b.pins.size(); i++)
    if (b.pins[i].role == ROLE_SD_BUS) have_slot = true;
  if (!have_slot) {
    fault(SEV_ERROR, "SD_NO_SLOT", "SD_MMC pins configured, but this board has no card socket");
    return;
  }
  for (int i = 0; i < 6; i++) {
    if (got[i] < 0) continue;
    if (got[i] != want[i])
      fault(SEV_ERROR, "SD_PIN",
            "SD_MMC %s assigned to GPIO%d, but this board wires it to GPIO%d",
            label[i], got[i], want[i]);
  }
}

bool sd_begin(bool mode_1bit) {
  State& s = S();
  charge_us(12000);                    // card identification and init
  int pwr = gpio_with_role(ROLE_SD_PWR_EN);
  if (pwr >= 0 && !(s.level.count(pwr) && s.level[pwr] == 1)) {
    fault(SEV_ERROR, "SD_UNPOWERED",
          "SD_MMC.begin() with SD_PWR_EN (GPIO%d) low -- the card supply is switched through "
          "Q2/Q3 and off at reset, so there is no card on the bus to answer",
          pwr);
    return false;
  }
  // The check inverted with the board. It used to warn about 1-bit mode,
  // because all four data lines were wired. D1/D2/D3 went to the second CAN
  // controller's SPI bus, so 4-bit is now the mistake: the card would be
  // clocked on three lines that end at a pull-up.
  if (!mode_1bit && gpio_with_role(ROLE_SD_BUS) >= 0) {
    int n = 0;
    const Board& bd = board();
    for (size_t i = 0; i < bd.pins.size(); i++)
      if (bd.pins[i].role == ROLE_SD_BUS) n++;
    if (n < 4)
      fault(SEV_ERROR, "SD_4BIT",
            "SD_MMC.begin() asked for 4-bit mode, but this board only wires %d of the "
            "data lines to the MCU -- D1/D2/D3 stop at the card. The mount will fail or "
            "read garbage on three of four lanes",
            n);
  }
  s.sd_begun = true;
  return true;
}

void sd_end() { S().sd_begun = false; }
bool sd_ready() { return S().sd_begun; }

void sd_open() {
  State& s = S();
  if (!s.sd_begun) {
    fault(SEV_ERROR, "SD_NOT_MOUNTED", "a file was opened before SD_MMC.begin() succeeded");
    return;
  }
  s.sd_open_flag = true;
  s.sd_unflushed_bytes = 0;
  charge_us(3000);
}

void sd_write(size_t bytes) {
  State& s = S();
  s.sd_unflushed_bytes += bytes;
  charge_us(40 + bytes / 8);
}

void sd_flush() {
  State& s = S();
  charge_us((uint64_t)((s.sd_stalled ? 300.0 : s.sd_flush_ms) * 1000.0));
  s.sd_unflushed_bytes = 0;
}

void sd_close() {
  State& s = S();
  sd_flush();
  charge_us(4000);
  s.sd_open_flag = false;
}

bool sd_file_open() { return S().sd_open_flag; }
size_t sd_unflushed() { return S().sd_unflushed_bytes; }
void sd_set_flush_ms(double ms) { S().sd_flush_ms = ms; }
void sd_set_stalled(bool st) { S().sd_stalled = st; }

// ===========================================================================
// Run control

bool run_should_stop() {
  State& s = S();
  if (s.stop) return true;
  if (s.t_us / 1000.0 >= s.duration_ms) {
    run_stop("scenario duration reached");
    return true;
  }
  return false;
}

void run_stop(const char* why) {
  State& s = S();
  if (s.stop) return;
  s.stop = true;
  s.stop_reason = why ? why : "";
  if (s.pwr_fail_at_ms >= 0.0 && !s.pwr_fail_observed)
    fault(SEV_ERROR, "PWR_FAIL_IGNORED",
          "PWR_FAIL (GPIO%d) asserted at %.1f ms and firmware never read it or attached an "
          "interrupt to it. The hardware buys a %.0f ms window to shed the sensor rail and close "
          "the file; nothing in this firmware spends it",
          gpio_with_role(ROLE_PWR_FAIL), s.pwr_fail_at_ms, board().ridethru_shed_ms);
  if (s.lost && s.sd_open_flag)
    fault(SEV_ERROR, "SD_OPEN_AT_POWER_LOSS",
          "the rails collapsed with a log file still open and %u bytes unflushed -- this is the "
          "corrupted-card case the ride-through capacitors exist to prevent",
          (unsigned)s.sd_unflushed_bytes);
}

const char* run_stop_reason() { return S().stop_reason.c_str(); }

// ===========================================================================
// Trace

namespace {

void trace_emit() {
  State& s = S();
  const char* ble = !s.ble_up ? "down" : (s.ble_sub ? "subscribed" : (s.ble_conn ? "connected" : (s.ble_adv ? "advertising" : "idle")));
  int sens = gpio_with_role(ROLE_SENS_EN);
  fprintf(s.trace,
          "%.3f,%.3f,%.3f,%d,%d,%.1f,%lu,%lu,%lu,%d,%d,%d,%d,%lu,%lu,%.4f,%s,%d,%d,%u,%.4f\n",
          s.t_us / 1000.0,
          s.v5,
          s.vbat,
          s.pwr_fail ? 1 : 0,
          (sens >= 0 && s.level.count(sens)) ? s.level[sens] : 0,
          s.gen_rpm,
          s.generated, s.delivered, s.dropped,
          s.led_lit, s.led_first.r, s.led_first.g, s.led_first.b,
          s.led_frames,
          s.notifies, s.last_notify,
          ble,
          s.adc_last_mv,
          s.sd_open_flag ? 1 : 0,
          (unsigned)s.sd_unflushed_bytes,
          s.reserve);
}

}  // namespace

void trace_open(const std::string& path) {
  State& s = S();
  s.trace = fopen(path.c_str(), "w");
  if (!s.trace) return;
  fprintf(s.trace,
          "t_ms,v5,vbat,pwr_fail,sens_en,cmd_rpm,can_generated,can_delivered,can_dropped,"
          "leds_lit,led_r,led_g,led_b,led_frames,notifies,notify_v,ble,adc_mv,"
          "sd_open,sd_unflushed,reserve\n");
  s.trace_next_us = 0;
}

void trace_close() {
  State& s = S();
  if (s.trace) { trace_emit(); fclose(s.trace); s.trace = 0; }
}

// ===========================================================================
// Scenario

namespace {

double num(const std::vector<std::string>& a, size_t i, double dflt) {
  if (i >= a.size()) return dflt;
  return atof(a[i].c_str());
}

uint32_t hexnum(const std::string& t) {
  return (uint32_t)strtoul(t.c_str(), 0, 0);
}

void apply_event(const std::vector<std::string>& a) {
  if (a.empty()) return;
  const std::string& c = a[0];
  if (c == "rpm") can_set_rpm(num(a, 1, 0));
  else if (c == "canrate") can_set_generator(S().gen_id, num(a, 1, 0));
  else if (c == "canid") can_set_generator(hexnum(a[1]), S().gen_hz);
  else if (c == "canframe") {
    Frame f;
    memset(&f, 0, sizeof f);
    f.id = hexnum(a[1]);
    f.dlc = (uint8_t)num(a, 2, 8);
    for (size_t i = 3; i < a.size() && i - 3 < 8; i++)
      f.data[i - 3] = (uint8_t)hexnum(a[i]);
    can_inject(f);
  }
  else if (c == "busoff") can_force_bus_off();
  else if (c == "sensor") sensor_set((int)num(a, 1, 1), num(a, 2, 0));
  else if (c == "sensorrail") sensor_set_rail((int)num(a, 1, 1), a.size() > 2 && a[2] == "5vs" ? RAIL_5VS : RAIL_NONE);
  else if (c == "range") {
    int ch = (int)num(a, 1, 1);
    const std::string& r = a.size() > 2 ? a[2] : std::string("r5v");
    double v = r == "bypass" ? R_BYPASS : (r == "r16v" ? R_16V : R_5V);
    if (ch >= 1 && ch < 9) S().chan_ratio[ch] = v;
  }
  else if (c == "vbat") power_set_vbat(num(a, 1, 13.8));
  // "usb 0" is what an ignition-off looks like now. "vbat 0" is unplugging
  // the OBD lead, which kills CAN, K-line and the battery reading but leaves
  // the board running -- a genuinely different event, and now a separate one.
  else if (c == "usb") power_set_v5(num(a, 1, 4.70));
  else if (c == "ads") ads_set_present(num(a, 1, 1) != 0);
  else if (c == "sdstall") sd_set_stalled(num(a, 1, 1) != 0);
  else if (c == "sdflush") sd_set_flush_ms(num(a, 1, 18));
  else if (c == "budget") power_set_budget(num(a, 1, 2500), num(a, 2, 769));
  else if (c == "ble") {
    const std::string& w = a.size() > 1 ? a[1] : std::string();
    if (w == "connect") ble_event_connect();
    else if (w == "disconnect") ble_event_disconnect();
    else if (w == "subscribe") ble_event_subscribe((uint16_t)num(a, 2, 1));
    else if (w == "unsubscribe") ble_event_subscribe(0);
    else if (w == "hwmode") ble_event_hwmode((uint8_t)num(a, 2, 0));
    else if (w == "initfail") ble_set_init_ok(num(a, 2, 1) == 0);
  }
  else if (c == "note") { /* documentation only */ }
  else fault(SEV_WARN, "SCENARIO", "unknown command '%s'", c.c_str());
}

bool ev_less(const Event& x, const Event& y) {
  if (x.t_us != y.t_us) return x.t_us < y.t_us;
  return x.seq < y.seq;
}

}  // namespace

void scenario_load(const std::string& path) {
  State& s = S();
  std::ifstream in(path.c_str());
  if (!in) { fprintf(stderr, "fwsim: cannot open scenario %s\n", path.c_str()); exit(2); }

  std::string line;
  size_t seq = 0;
  while (std::getline(in, line)) {
    size_t hash = line.find('#');
    if (hash != std::string::npos) line = line.substr(0, hash);
    std::istringstream ls(line);
    std::vector<std::string> tok;
    std::string t;
    while (ls >> t) tok.push_back(t);
    if (tok.empty()) continue;

    if (tok[0][0] == '@') {
      Event e;
      e.t_us = (uint64_t)(atof(tok[0].c_str() + 1) * 1000.0);
      e.argv.assign(tok.begin() + 1, tok.end());
      e.seq = seq++;
      s.events.push_back(e);
    } else if (tok[0] == "board" && tok.size() > 1) {
      s.board_request = tok[1];
    } else if (tok[0] == "duration" && tok.size() > 1) {
      s.duration_ms = atof(tok[1].c_str());
    } else if (tok[0] == "trace" && tok.size() > 1) {
      s.trace_ms = atof(tok[1].c_str());
    } else {
      fprintf(stderr, "fwsim: bad scenario line: %s\n", line.c_str());
      exit(2);
    }
  }
  std::stable_sort(s.events.begin(), s.events.end(), ev_less);

  const Board* b = board_by_name(s.board_request.empty() ? "autosport" : s.board_request);
  if (!b) { fprintf(stderr, "fwsim: unknown board '%s'\n", s.board_request.c_str()); exit(2); }
  set_board(b);

  // Events stamped at t=0 land before setup() runs, which is what a scenario
  // means by "the harness already had 13.8 V on it when the board booted".
  while (s.ev_next < s.events.size() && s.events[s.ev_next].t_us == 0) {
    apply_event(s.events[s.ev_next].argv);
    s.ev_next++;
  }
}

double scenario_duration_ms() { return S().duration_ms; }
double scenario_trace_ms() { return S().trace_ms; }

unsigned long sim_heap() { return S().heap; }

}  // namespace sim
