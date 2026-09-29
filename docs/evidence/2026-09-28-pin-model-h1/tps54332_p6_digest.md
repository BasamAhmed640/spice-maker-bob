# TPS54332 datasheet, PDF page 6: electrical characteristics, read by documents/digest.py

Input: tps54332_datasheet.pdf (sha256 in HASHES.txt; the PDF is not committed).
### Page 6

| Parameter | Conditions | MIN | TYP | MAX | Unit |
|---|---|---|---|---|---|
| Internal under voltage lockout threshold | Rising and Falling |  |  | 3.5 | V |
| Shutdown supply current | EN = 0 V , VIN = 12 V , –40°C to 85°C |  | 1 | 4 | μA |
| Operating – non switching supply current | VSENSE = 0.85 V |  | 82 | 120 | μA |
| Enable threshold | Rising and Falling |  | 1.25 | 1.35 | V |
| Input current | Enable threshold – 50 mV |  | -1 |  | μA |
| Input current | Enable threshold + 50 mV |  | -4 |  | μA |
| V oltage reference |  | 0.772 | 0.8 | 0.828 | V |
| On resistance | BOOT -PH = 3 V , VIN = 3.5 V |  | 115 | 200 | mΩ |
| On resistance | BOOT -PH = 6 V , VIN = 12 V |  | 80 | 150 | mΩ |
| Error amplifier transconductance (gm) | –2 μA < ICOMP < 2 μA, V(COMP) = 1 V |  | 92 |  | μmhos |
| Error amplifier DC gain( 1) | VSENSE = 0.8 V |  | 800 |  | V/V |
| Error amplifier unity gain bandwidth | 5 pF capacitance from COMP to GND pins |  | 2.7 |  | MHz |
| Error amplifier source/sink current | V(COMP) = 1.0 V , 100-mV overdrive |  | ±7 |  | μA |
| Switch current to COMP transconductance | VIN = 12 V |  | 12 |  | A /V |
| Pulse-skipping Eco-mode switch current threshold |  |  | 160 |  | mA |
| Current limit threshold | VIN = 12 V | 4.2 | 6.5 |  | A |
| Thermal Shutdown |  |  | 165 |  | °C |
| Charge current | V(SS) = 0.4 V |  | 2 |  | μA |
| SS to VSENSE matching | V(SS) = 0.4 V |  | 10 |  | mV |
| TPS54332 Switching Frequency VIN = 12 V | 25°C | 800 | 1000 | 1200 | kHz |
| Minimum controllable on time VIN = 12 V | 25°C |  | 110 | 135 | ns |
| Maximum controllable duty ratio BOOT -PH | = 6 V | 90% | 93% |  |  |

