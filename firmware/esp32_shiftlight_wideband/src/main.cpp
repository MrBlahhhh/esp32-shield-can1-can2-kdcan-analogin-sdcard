// Shift light + wideband BLE bridge + microSD logger — ESP32-S3 autosport board
//
// Ported from the MINI R53 sketch that runs on a Waveshare ESP32-S3-Zero
// (mini-r53-logger, firmware/esp32_shiftlight_wideband). A verbatim copy of
// that original is kept in firmware/vendor/ and is the control build for
// gen/simulate_firmware.py; every difference below is a difference in the
// hardware, not a change of mind about the logic.
//
// What the board changes, and why each line here is what it is:
//
//   1. Every pin moves. GPIO map is README section 6, and it is the contract.
//      The map is grouped by which side of the carrier each circuit sits on,
//      because the two 22-way sockets slot every plane layer: analog, CAN and
//      the SD bus all hang off the J1 row, K-line and I2C off the J3 row.
//   2. The wideband divider is 15k/26k, not 10k/10k. DIVIDER_GAIN was 2.0 and
//      is now 1.7334 — the original constant reads 17 % high here, and the
//      firmware's own 5.5 V clamp then hides the top of the range.
//   3. The +5 V sensor excitation is permanently on, fed from USB through a
//      polyfuse. The parent board switched it so firmware could shed 80 mA
//      and stretch a 127 ms ride-through; the supercap bank here holds
//      seconds, so the switch and its GPIO went away.
//   4. WS2812 data goes through a 74AHCT1G125 whose input floats from reset,
//      so GPIO48 is driven low before the LED driver ever touches it.
//   5. There is a power-fail signal and roughly 2.5 s of supercap behind it,
//      and a microSD card that will be left corrupt if that window is not
//      spent. README section 2 states the contract; shutdown() honours it.
//      The signal now watches the 5 V USB rail, not a 12 V harness.
//   6. K-line (ISO 9141 / KWP2000) is wired to GPIO41/42 through a discrete
//      low-side FET and a divider. TX is inverted in hardware, so the UART
//      needs UART_SIGNAL_TXD_INV. Nothing in this sketch drives it yet.
//
// GATT is unchanged, so the existing WidebandBleManager in the logger app
// pairs with this board without modification.
// Libraries: FastLED 3.7+, NimBLE-Arduino 2.x. CAN uses the core's TWAI driver.

#include <Arduino.h>
#include <FastLED.h>
#include <NimBLEDevice.h>
#include <Wire.h>
#include <SD_MMC.h>
#include <driver/twai.h>
#include <math.h>

// --- Pin map (README section 6) ---------------------------------------------
#define LED_PIN     48            // via U6 74AHCT1G125 to the WS2812 header
#define NUM_LEDS    8
#define LED_TYPE    WS2812B
#define COLOR_ORDER GRB

#define CAN_TX_PIN  GPIO_NUM_17
#define CAN_RX_PIN  GPIO_NUM_18
#define CAN_S_PIN   16            // low = normal, high = listen-only

#define PWR_FAIL_PIN 15           // rising = the 5 V rail is below 4.20 V

// K-line, not yet driven. Listed so the pin map in this file stays the whole
// pin map -- a GPIO that is documented nowhere is a GPIO someone reuses.
#define K_TX_PIN      2           // low-side FET gate; UART TX must be inverted
#define K_RX_PIN      1           // divided 22k/10k and clamped

// Second CAN bus: an MCP2518FD on SPI, because the ESP32-S3 has exactly one
// TWAI peripheral and two buses have to be watched at once. Not driven by
// this sketch yet; listed so the pin map in this file stays the whole pin
// map. SDI/SDO on the controller are named from ITS point of view, so they
// cross over: the controller's SDI is this end's MOSI.
#define CAN2_SCK_PIN  39
#define CAN2_MOSI_PIN 40          // -> MCP2518FD SDI
#define CAN2_MISO_PIN 41          // <- MCP2518FD SDO
#define CAN2_CS_PIN   42
#define CAN2_INT_PIN  21
// These two live on the far socket row from the card itself. They are the
// only SD signals that do, and deliberately: they are DC, so crossing the
// board costs nothing, while the six bus lines stay beside the slot.
#define SD_PWR_EN_PIN 47
#define SD_CD_PIN     11
// 1-BIT SDMMC. D1/D2/D3 are not wired to the MCU at all: those three pins
// went to the second CAN controller's SPI bus, which needed five and had
// two spare. The card still pulls all three up -- a card samples DAT3 at
// power-up and falls into SPI mode if it finds it low -- they just stop
// there. 1-bit carries about 1.5 MB/s against this logger's <130 kB/s, and
// bus width does not touch the card's internal write stall, which is the
// thing the whole hold-up budget is sized around.
#define SD_CLK_PIN 14
#define SD_CMD_PIN 13
#define SD_D0_PIN  12

#define RPM_STALE_MS 2000         // blank the strip if CAN goes quiet

// 500 kbit/s, id 0x316, RPM in bytes 2-3 little-endian / 6.4. Lifted from the
// working car sketch, esp32-canbus-SN65HVD230-v2 — same decode, same bus.
#define CAN_RPM_ID 0x316

CRGB leds[NUM_LEDS];
uint16_t rpm = 0;
bool redBlinkState = false;
unsigned long lastBlinkTime = 0;
const unsigned long blinkInterval = 100;   // 100 ms = 5 Hz blink above 7100 rpm
unsigned long lastRpmMs = 0;
bool canUp = false;

unsigned long canFrames = 0;      // frames since the last status line

// --- Analog front end (README section 3) ------------------------------------
//
// The four channels are identical and jumper-selected. AIN1 carries the
// wideband, and the gain below MUST match the jumper actually fitted:
//
//   RANGE open, BYPASS closed   0–3.3 V   ratio 1.0000   gain 1.0000
//   RANGE A,    BYPASS open     0–5.0 V   ratio 0.5769   gain 1.7334  <- default
//   RANGE B,    BYPASS open     0–16 V    ratio 0.1673   gain 5.9773
//
// The RANGE jumper now ships BRIDGED 1-2 (0–5 V), so this constant matches a
// board straight out of the box. Change it only if you cut that bridge.
//
// This is the constant that was wrong on the first port: 2.0 is the reciprocal
// of the R53 board's 10k/10k, and using it here reads 17 % high all the way up
// the range. gen/simulate_firmware.py study 3 measures it end to end.
// 13.21 / 2.21. One divider ratio on this board now, spanning 0-16 V, so a
// 5 V sensor lands at 0.836 V and a 12 V one at 2.01 V. The 0-5 V range and
// its 1.7334 went with the range-select jumpers -- see the note above the
// channel loop in gen/generate_schematic.py for why one fixed ratio is what
// makes the differential ground correction exact.
static const float DIVIDER_GAIN = 5.9774f;
static const int   ESP_ADC_PIN  = 4;       // AIN1 = GPIO4 = ADC1_CH3
static const int   VBAT_ADC_PIN = 8;       // VBAT_SNS = GPIO8, from OBD-II pin 16
// 108.2k / 8.2k, not the 11.0 this file inherited. The schematic moved to
// 13.2:1 so a 36 V input would not saturate the ADC and the constant never
// followed, so every battery reading was 20 % low. The fwsim board model
// carried the same wrong 1/11, which is why nothing caught it: the two agreed
// with each other and neither agreed with the board.
static const float VBAT_DIVIDER = 13.195f;

// I2C is on the Qwiic header, not the pins the R53 board used — GPIO7 and
// GPIO8 are the microSD supply enable and card detect here.
// IO38 is the DevKitC-1 v1.1 onboard RGB LED; SDA lives on IO47 here.
static const int   ADS_SDA_PIN  = 10;
static const int   ADS_SCL_PIN  =  9;
static const uint8_t ADS_ADDR   = 0x48;
static const float   ADS_LSB_MV = 0.125f;  // GAIN_ONE (+-4.096 V)

// The sensor can legitimately reach the top of its range, so the clamp has to
// sit above it rather than at 5.5 V, which on this front end is inside the
// measurement band once the gain is right.
static const float SENSOR_VMAX = 5.6f;

enum HwMode : uint8_t { HW_RESISTOR = 0, HW_ADS1115 = 1 };

static const char* DEVICE_NAME = "R53-Wideband";
static const char* SERVICE_UUID      = "4fafc201-1fb5-459e-8fcc-c5c9c331914b";
static const char* VOLTS_CHAR_UUID   = "beb5483e-36e1-4688-b7f5-ea07361b26a8";
static const char* HW_MODE_CHAR_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a9";

// The strip is redrawn every LOOP_PERIOD_MS; the wideband is notified more
// slowly. 50 Hz notifications outrun the connection interval a phone actually
// negotiates, which backs up NimBLE's buffers until the link drops — and the
// sensor itself only responds in ~100 ms, so 20 Hz loses nothing.
static const uint32_t LOOP_PERIOD_MS   = 20;
static const uint32_t NOTIFY_PERIOD_MS = 50;
static const uint32_t BLE_RETRY_MS     = 5000;
static const uint32_t LOG_FLUSH_MS     = 1000;

NimBLECharacteristic* voltsCharacteristic  = nullptr;
NimBLECharacteristic* hwModeCharacteristic = nullptr;
bool clientConnected = false;
bool bleReady        = false;
uint32_t bleRetryAtMs = 0;
uint32_t lastNotifyMs = 0;
volatile bool voltsSubscribed = false;

volatile uint8_t hwMode = HW_RESISTOR;
bool adsPresent = false;
bool adsTried   = false;

// --- Power fail --------------------------------------------------------------
//
// Set from the ISR, read from loop(). The ISR used to drop SENS_EN here as
// well, to double a 53 ms budget to 108 ms. There is no sensor switch any
// more and the budget is measured in seconds, so the ISR does the one thing
// it still has to: latch the flag and get out.
volatile bool powerFailed = false;
bool shuttingDown = false;

File logFile;
bool sdUp = false;
uint32_t lastLogFlushMs = 0;
unsigned long logLines = 0;

void updateLEDs();
bool bleInit();
void bleWatchdog();
void shutdown();

void IRAM_ATTR onPowerFail() {
  powerFailed = true;
}

/**
 * One status line a second — the only routine output, and the thing that says
 * which half is unhappy without a scope. Frames counts EVERY id on the bus, so
 * "frames 0" is a wiring/transceiver problem while "frames high, rpm 0" is a
 * decode problem.
 */
void printStatus() {
  static unsigned long last = 0;
  unsigned long now = millis();
  if (now - last < 1000) return;
  last = now;
  const char* ble;
  if (!bleReady)              ble = "DOWN";
  else if (clientConnected)   ble = "connected";
  else if (NimBLEDevice::getAdvertising()->isAdvertising()) ble = "advertising";
  else                        ble = "IDLE";
  Serial.printf("rpm %4u  can %s frames/s %-4lu  ble %-11s  sd %s lines %lu\n",
                rpm, canUp ? "up" : "DOWN", canFrames, ble,
                sdUp ? "up" : "DOWN", logLines);
  canFrames = 0;
}

// --- CAN --------------------------------------------------------------------
void canInit() {
  // CAN_S low = normal mode. R40 already pulls it there so the transceiver is
  // sane while the MCU is in reset; drive it anyway so the state is the
  // firmware's decision and not a resistor's.
  pinMode(CAN_S_PIN, OUTPUT);
  digitalWrite(CAN_S_PIN, LOW);

  twai_general_config_t g =
      TWAI_GENERAL_CONFIG_DEFAULT(CAN_TX_PIN, CAN_RX_PIN, TWAI_MODE_NORMAL);
  g.rx_queue_len = 32;   // FastLED.show() blocks ~0.5 ms; don't drop frames in it
  twai_timing_config_t t = TWAI_TIMING_CONFIG_500KBITS();
  twai_filter_config_t f = TWAI_FILTER_CONFIG_ACCEPT_ALL();

  if (twai_driver_install(&g, &t, &f) != ESP_OK || twai_start() != ESP_OK) {
    Serial.println("TWAI: init failed");
    return;
  }
  canUp = true;
  Serial.println("TWAI initialized");
}

void canPoll() {
  if (!canUp) return;
  twai_message_t msg;
  while (twai_receive(&msg, 0) == ESP_OK) {
    canFrames++;
    if (msg.extd || msg.rtr || msg.identifier != CAN_RPM_ID) continue;
    if (msg.data_length_code < 4) continue;
    uint16_t raw = msg.data[2] | ((uint16_t)msg.data[3] << 8);
    rpm = (uint16_t)constrain(raw / 6.4f, 0.0f, 9000.0f);
    lastRpmMs = millis();
  }
  twai_status_info_t st;
  if (twai_get_status_info(&st) == ESP_OK && st.state == TWAI_STATE_BUS_OFF) {
    twai_initiate_recovery();
  }
}

// --- Analog ------------------------------------------------------------------
float readEspAdcSensorVolts() {
  float v = (analogReadMilliVolts(ESP_ADC_PIN) / 1000.0f) * DIVIDER_GAIN;
  return constrain(v, 0.0f, SENSOR_VMAX);
}

float readBatteryVolts() {
  return (analogReadMilliVolts(VBAT_ADC_PIN) / 1000.0f) * VBAT_DIVIDER;
}

bool adsWriteReg(uint8_t reg, uint16_t value) {
  Wire.beginTransmission(ADS_ADDR);
  Wire.write(reg);
  Wire.write((uint8_t)(value >> 8));
  Wire.write((uint8_t)(value & 0xFF));
  return Wire.endTransmission() == 0;
}

bool adsReadReg(uint8_t reg, uint16_t* out) {
  Wire.beginTransmission(ADS_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(ADS_ADDR, (uint8_t)2) != 2) return false;
  uint16_t hi = Wire.read();
  uint16_t lo = Wire.read();
  *out = (hi << 8) | lo;
  return true;
}

/** Lazy I2C — only touch the bus when the phone asks for ADS1115 mode. */
void ensureAdsProbed() {
  if (adsTried) return;
  adsTried = true;
  Wire.begin(ADS_SDA_PIN, ADS_SCL_PIN);
  Wire.setClock(400000);
  // 0x1283, not 0x4283: MUX 001 is AIN0 referenced to AIN3, not to GND.
  //
  // AIN3 on both parts carries AGND_SENSE -- the sensor loom's own ground,
  // brought back as a Kelvin wire through an attenuator identical to the
  // signal channels'. The board is grounded twice (through the loom, and
  // through USB by way of a charger somewhere else in the car), and a few
  // hundred millivolts of chassis drop between those points is 6 % of a
  // 0-5 V channel. Reading differentially subtracts it exactly, because both
  // legs are divided by the same 15/26.
  //
  // Read single-ended (0x4283) and the offset is measured as signal. The
  // 0.1 % divider resistors on this board are wasted money if this register
  // is wrong.
  if (!adsWriteReg(0x01, 0x1283)) return;  // AIN0-AIN3, +-4.096 V, 128 SPS
  delay(10);
  uint16_t check = 0;
  adsPresent = adsReadReg(0x01, &check);
}

float readAdsSensorVolts() {
  ensureAdsProbed();
  if (!adsPresent) return NAN;
  uint16_t raw = 0;
  if (!adsReadReg(0x00, &raw)) return NAN;
  float v = (((int16_t)raw) * ADS_LSB_MV / 1000.0f) * DIVIDER_GAIN;
  return constrain(v, 0.0f, SENSOR_VMAX);
}

float readSensorVolts() {
  if (hwMode == HW_ADS1115) {
    float v = readAdsSensorVolts();
    if (isfinite(v)) return v;
  }
  return readEspAdcSensorVolts();
}

// --- microSD -----------------------------------------------------------------
void sdInit() {
  pinMode(SD_CD_PIN, INPUT);
  pinMode(SD_PWR_EN_PIN, OUTPUT);
  digitalWrite(SD_PWR_EN_PIN, HIGH);       // the card supply is switched
  delay(10);                               // let the rail come up before clocking

  SD_MMC.setPins(SD_CLK_PIN, SD_CMD_PIN, SD_D0_PIN);
  if (!SD_MMC.begin("/sdcard", true)) {    // 1-bit; see the pin block above
    Serial.println("SD: mount failed");
    return;
  }
  logFile = SD_MMC.open("/log.csv", FILE_APPEND);
  if (!logFile) {
    Serial.println("SD: could not open /log.csv");
    return;
  }
  logFile.println("t_ms,rpm,wideband_v,vbat_v");
  sdUp = true;
  Serial.println("SD: logging to /log.csv");
}

void logSample(float volts, float vbat) {
  if (!sdUp || shuttingDown) return;
  logFile.printf("%lu,%u,%.3f,%.2f\n", millis(), rpm, volts, vbat);
  logLines++;
  uint32_t now = millis();
  if (now - lastLogFlushMs >= LOG_FLUSH_MS) {
    lastLogFlushMs = now;
    logFile.flush();     // bound what an unexpected reset can lose to one second
  }
}

/**
 * The firmware half of README section 2. The hardware guarantees roughly
 * 108 ms between PWR_FAIL and the converters dropping out; this is what spends
 * it. Order matters: the sensor rail is already off (the ISR did it), the strip
 * goes dark so it is not drawing from the bank, then the file is closed, and
 * only then is the card supply removed — with SD_MMC.end() first, because
 * dropping SD_PWR_EN while the bus pins are still driven back-feeds the card
 * through its ESD structures (README section 5).
 */
void shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;

  // The LEDs are the one load worth shedding: eight WS2812s at full white is
  // 480 mA against the ~120 mA the rest of the board draws, and the hold-up
  // budget is inversely proportional to it. Clearing them first turns a
  // 769 ms worst case back into 2500 ms.
  FastLED.clear(true);

  if (sdUp) {
    // close() flushes. Calling flush() first as well costs a second full
    // card-side write, and on this budget that is not a tidiness question:
    // gen/simulate_firmware.py study 11 measures the double flush cutting the
    // tolerable card latency from ~100 ms to ~50 ms.
    logFile.close();
    SD_MMC.end();
    sdUp = false;
  }
  digitalWrite(SD_PWR_EN_PIN, LOW);

  Serial.printf("PWR_FAIL at %lu ms: %lu lines flushed and closed\n", millis(), logLines);
}

// --- BLE ---------------------------------------------------------------------
class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* server, NimBLEConnInfo& info) override {
    clientConnected = true;
    // Ask for a link the phone can hold: 15–30 ms interval, no slave latency,
    // 4 s supervision timeout. The default timeout can run to 20 s, and while
    // it counts down after a dropout the server is neither connected nor
    // advertising — twenty seconds of "no wideband bridge found" on the phone.
    server->updateConnParams(info.getConnHandle(), 12, 24, 0, 400);
  }
  void onDisconnect(NimBLEServer*, NimBLEConnInfo&, int) override {
    clientConnected = false;
    // Cleared here, not left to onSubscribe(0): a link that drops out of range
    // or times out never delivers an unsubscribe, and a stale true would have
    // us notifying into nothing until the next client happened to connect.
    voltsSubscribed = false;
    // Deliberately NOT calling startAdvertising() here: this runs on the NimBLE
    // host task while the connection is still being torn down.
    // advertiseOnDisconnect() restarts it at a safe point and bleWatchdog()
    // catches it if that ever doesn't take.
  }
};

class VoltsCallbacks : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic*, NimBLEConnInfo&, uint16_t subValue) override {
    voltsSubscribed = (subValue != 0);
  }
};

class HwModeCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* c, NimBLEConnInfo&) override {
    NimBLEAttValue v = c->getValue();
    if (v.length() < 1) return;
    uint8_t mode = v[0];
    if (mode == HW_RESISTOR || mode == HW_ADS1115) {
      hwMode = mode;
      c->setValue(&mode, 1);
    }
  }
};

/**
 * Bring the whole BLE stack up, checking every step. Returns false — leaving
 * the stack torn back down so the next attempt starts clean — if any of them
 * fails. Every one of these calls returns a status in NimBLE 2.x; ignoring them
 * is exactly how the board ends up running the shift light perfectly with no
 * BLE at all and nothing on the terminal to say why.
 */
bool bleInit() {
  voltsCharacteristic  = nullptr;
  hwModeCharacteristic = nullptr;

  if (!NimBLEDevice::init(DEVICE_NAME)) {
    Serial.println("BLE: NimBLEDevice::init() failed");
    return false;
  }
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);

  NimBLEServer* server = NimBLEDevice::createServer();
  if (server == nullptr) {
    Serial.println("BLE: createServer() failed");
    NimBLEDevice::deinit(true);
    return false;
  }
  server->setCallbacks(new ServerCallbacks());
  server->advertiseOnDisconnect(true);

  NimBLEService* service = server->createService(SERVICE_UUID);
  if (service == nullptr) {
    Serial.println("BLE: createService() failed");
    NimBLEDevice::deinit(true);
    return false;
  }
  voltsCharacteristic = service->createCharacteristic(
      VOLTS_CHAR_UUID, NIMBLE_PROPERTY::NOTIFY);
  hwModeCharacteristic = service->createCharacteristic(
      HW_MODE_CHAR_UUID, NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::WRITE);
  if (voltsCharacteristic == nullptr || hwModeCharacteristic == nullptr) {
    Serial.println("BLE: createCharacteristic() failed");
    voltsCharacteristic = nullptr;
    hwModeCharacteristic = nullptr;
    NimBLEDevice::deinit(true);
    return false;
  }
  voltsCharacteristic->setCallbacks(new VoltsCallbacks());
  hwModeCharacteristic->setCallbacks(new HwModeCallbacks());
  uint8_t hwModeInit = hwMode;
  hwModeCharacteristic->setValue(&hwModeInit, 1);
  voltsSubscribed = false;

  // The phone filters the scan on the 128-bit service UUID, so that UUID has to
  // be advertised or nothing will ever match. 18 bytes of UUID + 3 of flags
  // fills most of the 31-byte advert, which is why the name goes in the scan
  // response instead of competing for room with it.
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->setName(DEVICE_NAME);
  adv->addServiceUUID(SERVICE_UUID);
  adv->enableScanResponse(true);
  if (!adv->start()) {
    Serial.println("BLE: advertising failed to start");
    voltsCharacteristic = nullptr;
    hwModeCharacteristic = nullptr;
    NimBLEDevice::deinit(true);
    return false;
  }

  bleReady = true;
  Serial.printf("BLE: advertising as %s (free heap %u)\n",
                DEVICE_NAME, (unsigned)ESP.getFreeHeap());
  return true;
}

/**
 * Two failures this recovers from, both of which used to need a power cycle:
 * a bring-up that never succeeded (retry it), and a stack that is up but has
 * silently stopped advertising while nothing is connected (restart it).
 */
void bleWatchdog() {
  uint32_t now = millis();

  if (!bleReady) {
    if ((int32_t)(now - bleRetryAtMs) < 0) return;
    bleRetryAtMs = now + BLE_RETRY_MS;
    Serial.printf("BLE: retrying bring-up (free heap %u)\n", (unsigned)ESP.getFreeHeap());
    bleInit();
    return;
  }

  static uint32_t lastCheck = 0;
  if (now - lastCheck < 1000) return;
  lastCheck = now;
  if (!clientConnected && !NimBLEDevice::getAdvertising()->isAdvertising()) {
    Serial.println("BLE: advertising had stopped — restarting");
    NimBLEDevice::getAdvertising()->start();
  }
}

// -----------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  unsigned long serialWait = millis();
  while (!Serial && millis() - serialWait < 1500) delay(10);

  // GPIO48 reaches the 74AHCT1G125 through 33 ohms with no pull-down, so the
  // buffer input is undefined from reset until something drives it — a random
  // flicker on the strip and a milliamp of shoot-through in U6 (README
  // section 7, item 9). Drive it low before FastLED claims the pin.
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  // Armed before anything slow runs. An ignition cut during BLE bring-up is
  // still an ignition cut, and the card is not open yet but the interrupt
  // still has to be the thing that shuts the rail down.
  pinMode(PWR_FAIL_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(PWR_FAIL_PIN), onPowerFail, RISING);

  // BLE goes first, on purpose. The BT controller wants a sizeable block of
  // internal RAM (it cannot use PSRAM) and it is the one subsystem here that
  // fails silently — FastLED's RMT buffers and TWAI's rx queue both succeed on
  // whatever is left. Claiming the radio's memory before them turns "BLE
  // sometimes doesn't come up" into a problem the watchdog can retry rather
  // than one only a power cycle fixes.
  if (!bleInit()) {
    bleRetryAtMs = millis() + BLE_RETRY_MS;
    Serial.println("BLE: bring-up failed at boot — will keep retrying");
  }

  FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, NUM_LEDS);
  FastLED.setBrightness(75);
  FastLED.clear(true);

  // Warm the channel before setting attenuation (required on arduino-esp32 3.x).
  analogReadResolution(12);
  (void)analogRead(ESP_ADC_PIN);
  analogSetPinAttenuation(ESP_ADC_PIN, ADC_11db);
  analogSetPinAttenuation(VBAT_ADC_PIN, ADC_11db);

  canInit();
  sdInit();
}

void loop() {
  // Checked before anything else: from here on the only job is to finish the
  // write and stop. Nothing below this line may start new work.
  if (powerFailed) {
    shutdown();
    delay(LOOP_PERIOD_MS);
    return;
  }

  canPoll();
  bleWatchdog();

  float volts = readSensorVolts();

  // getSubscribedCount() — not just clientConnected. A phone is connected for
  // a beat before it discovers services and writes the CCCD, and notifying into
  // that gap is pure churn on the link at the least forgiving moment.
  if (bleReady && voltsCharacteristic != nullptr && voltsSubscribed &&
      (uint32_t)(millis() - lastNotifyMs) >= NOTIFY_PERIOD_MS) {
    lastNotifyMs = millis();
    if (isfinite(volts)) {
      voltsCharacteristic->setValue((uint8_t*)&volts, sizeof(volts));
      voltsCharacteristic->notify();
    }
  }

  logSample(volts, readBatteryVolts());

  updateLEDs();
  FastLED.show();
  printStatus();
  delay(LOOP_PERIOD_MS);
}

void updateLEDs() {
#ifdef RPM_STALE_MS
  if (rpm != 0 && (unsigned long)(millis() - lastRpmMs) > RPM_STALE_MS) rpm = 0;
#endif

  CRGB color;

  // Number of LED pairs to light, 0 to 4.
  int numPairs = constrain(map(rpm, 0, 7100, 0, 4), 0, 4);

  if (rpm < 3000) {
    color = CRGB(0, 0, 0);                // off below 3000 rpm
  } else if (rpm < 6000) {
    color = CRGB(0, 255, 0);              // solid green 3000-6000
  } else if (rpm <= 7100) {
    uint8_t t = map(rpm, 6000, 7100, 0, 255);   // green to red 6000-7100
    color = CRGB(t, 255 - t, 0);
  } else {
    if (millis() - lastBlinkTime >= blinkInterval) {
      redBlinkState = !redBlinkState;
      lastBlinkTime = millis();
    }
    color = redBlinkState ? CRGB(255, 0, 0) : CRGB(0, 0, 0);
    numPairs = 4;                         // all eight blink above 7100
  }

  // Light from the ends inward.
  for (int i = 0; i < NUM_LEDS; i++) {
    leds[i] = CRGB(0, 0, 0);
    if (numPairs >= 1 && (i == 0 || i == 7)) leds[i] = color;
    if (numPairs >= 2 && (i == 1 || i == 6)) leds[i] = color;
    if (numPairs >= 3 && (i == 2 || i == 5)) leds[i] = color;
    if (numPairs >= 4 && (i == 3 || i == 4)) leds[i] = color;
  }
}
