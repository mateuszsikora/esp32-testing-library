/* An Arduino-ESP32 sketch that takes the core's own driver paths: `Serial` (HardwareSerial on
 * UART0) for a banner and a line-based command interpreter, `Preferences` for its settings in NVS,
 * and, when the settings name a network, `WiFi` as a station and `WebServer` on port 80.
 *
 * The settings are in the Preferences namespace "shell", read once at boot: `greeting`, printed
 * after the banner, and `ssid` and `password`, the network to join. Without an `ssid` the sketch
 * stays off the air. A line on `Serial` is a command: `help`, `greeting`, `uptime` or `wifi`.
 * Every line the sketch prints starts with "arduino_shell: " and ends with println's "\r\n". */
#include <Arduino.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>

#define PREFIX "arduino_shell: "
// The longest command line kept; the rest of a longer line is dropped.
#define MAX_LINE 64

static Preferences preferences;
static WebServer server(80);
static String greeting;
static bool joining;
static bool serving;
static unsigned long requests;
static char line[MAX_LINE + 1];
static size_t length;

static void status() {
    requests++;
    String json = "{\"status\":\"ok\",\"requests\":" + String(requests) +
                  ",\"uptime_ms\":" + String(millis()) + "}";
    server.send(200, "application/json", json);
    Serial.println(PREFIX "GET /status -> 200");
}

static void run(const char *command) {
    if (strcmp(command, "help") == 0) {
        Serial.println(PREFIX "commands: help, greeting, uptime, wifi");
    } else if (strcmp(command, "greeting") == 0) {
        Serial.print(PREFIX "greeting=");
        Serial.println(greeting);
    } else if (strcmp(command, "uptime") == 0) {
        Serial.print(PREFIX "uptime_ms=");
        Serial.println(millis());
    } else if (strcmp(command, "wifi") == 0) {
        if (!joining) {
            Serial.println(PREFIX "wifi=off");
        } else if (WiFi.status() == WL_CONNECTED) {
            Serial.print(PREFIX "wifi=connected ip=");
            Serial.println(WiFi.localIP());
        } else {
            Serial.println(PREFIX "wifi=joining");
        }
    } else if (command[0] != '\0') {
        Serial.print(PREFIX "unknown command '");
        Serial.print(command);
        Serial.println("'; try help");
    }
}

// One byte of a command line: a line ends at '\n', and a '\r' before it is dropped.
static void take(int c) {
    if (c == '\n') {
        line[length] = '\0';
        if (length > 0 && line[length - 1] == '\r') line[length - 1] = '\0';
        run(line);
        length = 0;
    } else if (length < MAX_LINE) {
        line[length++] = (char)c;
    }
}

void setup() {
    Serial.begin(115200);
    Serial.println(PREFIX "Arduino-ESP32 " ESP_ARDUINO_VERSION_STR);
    // Read-only, so that the sketch writes nothing to NVS. On a flash without the namespace,
    // begin() fails and the core logs "nvs_open failed: NOT_FOUND"; every read then returns its
    // default.
    preferences.begin("shell", true);
    greeting = preferences.getString("greeting", "");
    String ssid = preferences.getString("ssid", "");
    String password = preferences.getString("password", "");
    preferences.end();
    Serial.print(PREFIX "greeting=");
    Serial.println(greeting);
    if (ssid.length() > 0) {
        joining = true;
        WiFi.mode(WIFI_STA);
        WiFi.begin(ssid.c_str(), password.c_str());
        Serial.print(PREFIX "joining ");
        Serial.println(ssid);
    }
    Serial.println(PREFIX "ready; type help");
}

void loop() {
    while (Serial.available() > 0) take(Serial.read());
    if (joining && !serving && WiFi.status() == WL_CONNECTED) {
        server.on("/status", HTTP_GET, status);
        server.begin();
        serving = true;
        Serial.print(PREFIX "listening on ");
        Serial.print(WiFi.localIP());
        Serial.println(" port 80");
    }
    if (serving) server.handleClient();
    delay(1);
}
