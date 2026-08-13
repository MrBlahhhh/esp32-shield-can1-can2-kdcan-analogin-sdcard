// Firmware-in-the-loop simulator core.
//
// The shims (Arduino.h, FastLED.h, NimBLEDevice.h, Wire.h, driver/twai.h) are
// thin: they translate an Arduino/ESP-IDF call into one of the calls below.
// Everything that decides what the hardware *does* lives here, so there is one
// place to read when a result looks wrong.
//
// Two ideas carry the whole thing:
//
//   1. Time is virtual. delay(20) does not sleep, it advances a counter, and
//      every modelled cost (a WS2812 refresh, an I2C transaction, an ADC
//      conversion) advances the same counter. A minute of car time runs in
//      milliseconds and runs identically every time.
//
//   2. The board is a model, not an assumption. A GPIO is not a number, it is
//      whatever the schematic hung on it. Reading an ADC on a pin that this
//      board uses as the microSD supply enable is not a value, it is a fault.

#ifndef SIM_H
#define SIM_H

#include <stdint.h>
#include <stddef.h>
#include <string>
#include <vector>

namespace sim {

// ---------------------------------------------------------------------------
// What a pin is wired to. The port hazards this simulator exists to find are
// all of the form "the sketch believes GPIO n is X, this board says it is Y".
enum Role {
  ROLE_UNUSED = 0,
  ROLE_ANALOG_IN,     // conditioned sensor channel
  ROLE_VBAT_SNS,      // battery monitor divider
  ROLE_CAN_TX,
  ROLE_CAN_RX,
  ROLE_CAN_S,         // transceiver silent-mode select
  ROLE_WS2812_DIN,
  ROLE_I2C_SDA,
  ROLE_I2C_SCL,
  ROLE_SD_PWR_EN,
  ROLE_SD_CD,
  ROLE_SD_BUS,
  ROLE_PWR_FAIL,
  ROLE_SENS_EN,
  ROLE_KLINE_TX,      // ISO 9141 K-line, low-side FET gate
  ROLE_KLINE_RX,      // ISO 9141 K-line, divided and clamped
  ROLE_USB,
  ROLE_UART,
  ROLE_SPI,
  ROLE_BOOT,
  ROLE_SPARE_STRAP,   // broken out, but a strapping pin
  ROLE_PSRAM          // physically unusable on this module
};

const char* role_name(Role r);

// Where a channel's sensor gets its excitation. A sensor on the switched rail
// reads zero until firmware raises SENS_EN, which is the single most likely
// way a correct-looking sketch reads nothing on this board.
enum Rail { RAIL_NONE = 0, RAIL_5VS };

struct PinDef {
  int   gpio;
  Role  role;
  const char* net;
  double ratio;   // ANALOG_IN only: pin_volts = source_volts * ratio
  Rail  rail;     // ANALOG_IN only: what powers the sensor
};

struct Board {
  std::string name;
  std::vector<PinDef> pins;
  double adc_fullscale;      // volts the ADC saturates at (11/12 dB attenuation)
  double adc_gain_err;       // fractional error the internal ADC contributes
  int    adc_bits;
  bool   ws2812_buffered;    // DIN goes through a 5 V CMOS buffer that floats at boot
  bool   has_pwr_fail;       // board has the TLV431 power-fail detector
  double pwr_fail_trip;      // volts on the supply rail, falling
  double pwr_fail_hyst;      // volts of hysteresis
  double supply_floor;       // volts at which the MCU stops running
  double ridethru_shed_ms;   // PWR_FAIL -> rail collapse, load shed
  double ridethru_noshed_ms; // ... with everything still running
};

const Board* board_by_name(const std::string& name);
const Board& board();
void set_board(const Board* b);

// ---------------------------------------------------------------------------
// Faults are the product of a run. Each is recorded once per (code, detail)
// pair no matter how many times the firmware repeats the offence, so a bug in
// a 20 ms loop does not bury the run in 3000 identical lines.
enum Severity { SEV_NOTE = 0, SEV_WARN, SEV_ERROR };

void fault(Severity sev, const char* code, const char* fmt, ...);
const std::vector<std::string>& faults();
int  fault_count(Severity sev);

// ---------------------------------------------------------------------------
// Virtual time.
uint64_t now_us();
void     advance_us(uint64_t us);   // runs the event queue and the CAN generator
void     charge_us(uint64_t us);    // modelled cost of an operation; same thing

// ---------------------------------------------------------------------------
// Pin state as the firmware sees it.
void   pin_mode(int gpio, int mode);
void   pin_write(int gpio, int level);
int    pin_read(int gpio);
bool   pin_driven(int gpio);        // has firmware ever driven it?
void   attach_isr(int gpio, void (*fn)(), int mode);

// ADC. Returns millivolts at the pin, after the board's divider, clipping and
// quantisation -- i.e. what the chip would actually convert.
int    adc_read_mv(int gpio);
int    adc_read_raw(int gpio);
void   adc_set_attenuation(int gpio, int atten);
void   adc_set_resolution(int bits);

// ---------------------------------------------------------------------------
// CAN. The generator emits one frame every period at the configured id with
// RPM encoded the way the R53 cluster encodes it; canframe() injects one raw
// frame for decode tests.
struct Frame {
  uint32_t id;
  bool     extd;
  bool     rtr;
  uint8_t  dlc;
  uint8_t  data[8];
};

void can_install(int tx_gpio, int rx_gpio, int mode, int rx_queue_len);
bool can_start();
bool can_receive(Frame* out);         // non-blocking
bool can_bus_off();
void can_recover();
void can_set_generator(uint32_t id, double hz);
void can_set_rpm(double rpm);
void can_inject(const Frame& f);
void can_force_bus_off();
unsigned long can_rx_dropped();

// ---------------------------------------------------------------------------
// LEDs.
struct Rgb { uint8_t r, g, b; };
void leds_attach(int gpio, void* buf, int count);
void leds_show(const Rgb* pixels, int count, uint8_t brightness);
int  leds_lit();
Rgb  leds_first_lit();
unsigned long leds_frames();

// ---------------------------------------------------------------------------
// I2C / ADS1115. The model answers at 0x48 only if the scenario says the part
// is fitted AND the firmware talked to the pins this board puts the bus on.
void i2c_begin(int sda_gpio, int scl_gpio);
bool i2c_write_reg(uint8_t addr, uint8_t reg, uint16_t value);
bool i2c_read_reg(uint8_t addr, uint8_t reg, uint16_t* out);
void ads_set_present(bool present);

// ---------------------------------------------------------------------------
// BLE. The stack is scriptable: the scenario decides whether bring-up succeeds,
// when a phone connects, and when it subscribes.
void ble_set_init_ok(bool ok);
bool ble_init(const char* name);
void ble_deinit();
bool ble_adv_start();
bool ble_adv_active();
void ble_note_notify(const void* data, size_t len);
unsigned long ble_notify_count();
float ble_last_notify_f32();
bool ble_connected();

struct BleHooks {                    // filled in by the NimBLE shim
  void (*on_connect)();
  void (*on_disconnect)();
  void (*on_subscribe)(uint16_t);
  void (*on_hwmode_write)(uint8_t);
};
void ble_set_hooks(const BleHooks& h);
void ble_event_connect();
void ble_event_disconnect();
void ble_event_subscribe(uint16_t v);
void ble_event_hwmode(uint8_t v);

// ---------------------------------------------------------------------------
// Power. The scenario drives the harness voltage; the model derives PWR_FAIL,
// the ride-through budget and, when the budget runs out, the end of the run.
void   power_set_vbat(double volts);       // OBD-II pin 16, sense only
void   power_set_v5(double volts);         // the rail everything runs on
// Override the board's ride-through window for one run (scenario: `budget`).
void   power_set_budget(double shed_ms, double noshed_ms);
double power_vbat();
double power_v5();
bool   power_failed();               // PWR_FAIL asserted
double power_reserve();              // 1.0 = full cap bank, 0.0 = rails gone
bool   power_lost();                 // rails have collapsed; stop the run
double power_fail_at_ms();

// Sensor sources, as presented to the board's input pins by the harness.
void   sensor_set(int channel, double volts);
void   sensor_set_rail(int channel, Rail rail);

// ---------------------------------------------------------------------------
// microSD, modelled only as far as the ride-through question needs: is there
// an open file with unflushed bytes at the moment the rails go?
void   sd_set_pins(int clk, int cmd, int d0, int d1, int d2, int d3);
bool   sd_begin(bool mode_1bit);
void   sd_end();
bool   sd_ready();
void   sd_open();
void   sd_write(size_t bytes);
void   sd_flush();
void   sd_close();
bool   sd_file_open();
size_t sd_unflushed();
void   sd_set_flush_ms(double ms);
void   sd_set_stalled(bool stalled);

// ---------------------------------------------------------------------------
// Serial capture.
void serial_out(const char* text);
const std::string& serial_log();

// ---------------------------------------------------------------------------
// Scenario and run control.
void   scenario_load(const std::string& path);
double scenario_duration_ms();
double scenario_trace_ms();
bool   run_should_stop();
void   run_stop(const char* why);
const char* run_stop_reason();

// The trace is written by the simulator, not the runner: rows have to land at
// a fixed sample rate even while the firmware is inside a delay(), and the
// runner only gets control back between loop() calls.
void trace_open(const std::string& path);
void trace_close();

unsigned long sim_heap();

}  // namespace sim

#endif  // SIM_H
