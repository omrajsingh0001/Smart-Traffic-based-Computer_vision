// ESP32 Smart Traffic Light Controller
// This script listens for 4-character strings over USB (Serial)
// Example format: "GRRR" (Lane 1 Green, Lanes 2,3,4 Red)         
// Example format: "YRRR" (Lane 1 Yellow, Lanes 2,3,4 Red)   

// --- Safe Pin Definitions ---
const int N_R = 13; const int N_Y = 14; const int N_G = 27; // North (Lane 1)
const int E_R = 26; const int E_Y = 25; const int E_G = 33; // East  (Lane 2)
const int S_R = 32; const int S_Y = 18; const int S_G = 19; // South (Lane 3)
const int W_R = 21; const int W_Y = 22; const int W_G = 23; // West  (Lane 4)

void setup() {
  // Start the USB Serial connection at a fast baud rate    
  Serial.begin(115200);

  // Set all pins as OUTPUT
  pinMode(N_R, OUTPUT); pinMode(N_Y, OUTPUT); pinMode(N_G, OUTPUT);              
  pinMode(E_R, OUTPUT); pinMode(E_Y, OUTPUT); pinMode(E_G, OUTPUT);
  pinMode(S_R, OUTPUT); pinMode(S_Y, OUTPUT); pinMode(S_G, OUTPUT);
  pinMode(W_R, OUTPUT); pinMode(W_Y, OUTPUT); pinMode(W_G, OUTPUT);

  // Initialize all lights to Red on startup for safety
  setLane(1, 'R'); setLane(2, 'R'); setLane(3, 'R'); setLane(4, 'R');
}

void loop() {
  // Check if the Python software sent any data over the USB cable
  if (Serial.available() > 0) {
    // Read the string until a newline character is received
    String command = Serial.readStringUntil('\n');
    command.trim(); // Remove any accidental spaces or hidden return characters
    
    // We expect exactly 4 characters (e.g., "GRRR", "RGRR", "RRGR", "RRRG")
    if (command.length() == 4) {
      setLane(1, command.charAt(0));
      setLane(2, command.charAt(1));
      setLane(3, command.charAt(2));
      setLane(4, command.charAt(3));
    }
  }
}

// Helper function to easily change the color of a specific lane
void setLane(int lane, char state) {   
  int rPin, yPin, gPin;
  
  // Map the lane number to the correct pins
  if (lane == 1)      { rPin = N_R; yPin = N_Y; gPin = N_G; }    
  else if (lane == 2) { rPin = E_R; yPin = E_Y; gPin = E_G; }
  else if (lane == 3) { rPin = S_R; yPin = S_Y; gPin = S_G; }
  else if (lane == 4) { rPin = W_R; yPin = W_Y; gPin = W_G; }
  else return; // Invalid lane, do nothing

  // 1. Turn all LEDs off first so colors don't mix
  digitalWrite(rPin, LOW);
  digitalWrite(yPin, LOW);
  digitalWrite(gPin, LOW);

  // 2. Turn on the requested color
  if (state == 'R' || state == 'r') {     
    digitalWrite(rPin, HIGH); 
  }
  else if (state == 'Y' || state == 'y' || state == 'O' || state == 'o') { 
    digitalWrite(yPin, HIGH); // Supports 'Y' (Yellow) or 'O' (Orange)            
  }
  else if (state == 'G' || state == 'g') { 
    digitalWrite(gPin, HIGH); 
  }
}
