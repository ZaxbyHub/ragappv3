# HydraProbe-9 Soil Sensor - Product Specification

Document class: manufacturer specification. Model: HydraProbe-9. Issued by Voss AgriTech, Hardware Division, 2030-05-19. This document is the current controlling specification for the HydraProbe-9 family.

## 1. Product overview

The HydraProbe-9 is a time-domain reflectometry soil sensor intended for continuous unattended deployment. It shares no components, firmware, or calibration standards with the Kellerman Instruments Aeris-M; the two product lines are unrelated and their specifications must never be intermixed.

## 2. Sampling and measurement

- Default sampling cadence: one reading every 10 minutes.
- Measurement principle: time-domain reflectometry along a fixed waveguide.
- Measurement depth: up to 90 cm below ground surface.
- Stated accuracy: plus or minus 1.1 percent volumetric water content.
- Operating range: -40 C to +70 C.

## 3. Calibration

- Calibration interval: 60 days under normal service.
- Method: factory bench calibration; field calibration is not supported.
- Calibration events are recorded automatically by the onboard controller.

## 4. Firmware

- Reference firmware: version 2.8.0.
- Updates are delivered over the air and validated against a signed manifest.

## 5. Power and battery life

- Power source: user-replaceable alkaline battery pack.
- Expected battery life: 10 years at the 10-minute default cadence.
- Replacement packs are shipped in cases of twelve.

## 6. Housing and handling

- Housing: glass-filled nylon, rated for buried service.
- The sensor head may be cleaned in the field with deionized water.
- No lithium cell is present anywhere in the HydraProbe-9; disposal follows ordinary electronics recycling.

## 7. Interchange warning

Distributors occasionally quote HydraProbe-9 figures when asked about Aeris-M deployments because both products use capacitive or reflectometry sensing in agriculture. Correct practice: any question about cadence, accuracy, calibration interval, battery life, or operating range must be answered from the specification of the model actually deployed at the site.
