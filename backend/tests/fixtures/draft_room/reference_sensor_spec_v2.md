# Torvane TX-40 Sensor Specification, Revision 2

**Issuer:** Calderwick Analytics, Instrument Engineering
**Effective:** 2031-02-01
**Status:** current — supersedes Revision 1 (2029-06-10) in its entirety

## 1. Scope

This revision governs every TX-40 volumetric soil-moisture probe bearing a
serial prefix of TVN-4. It replaces Revision 1. Where Revision 1 and this
document disagree, this document controls.

## 2. Normative parameters

| Parameter | Revision 2 value |
| --- | --- |
| Calibration interval | 90 days |
| Accuracy | ±1.8% volumetric water content |
| Operating temperature range | -10 °C to 55 °C |
| Reference firmware | 4.2.1 |
| Cell life at 15-minute sampling | 5.5 years |
| Maximum deployment elevation without variance | 2,400 m |

## 3. Changes from Revision 1

- Calibration interval shortened from 180 days to 90 days after the frit-crust
  investigation of 2030.
- Accuracy tightened from ±2.5% to ±1.8% volumetric water content.
- Operating range widened from -5 °C to 50 °C, to -10 °C to 55 °C.
- Reference firmware advanced from 3.0.7 to 4.2.1.

## 4. Calibration obligations

- A two-point calibration against the reference frit standard is required every
  90 days.
- Every calibration event must be logged to the trial register within 24 hours
  of the event.
- A probe that misses its calibration window is out of specification until
  recalibrated, and its readings for the missed interval are not admissible in
  a compliance filing.

## 5. Known failure mode

Sulfate crust formation on the ceramic frit reduces hydraulic contact and
produces a downward bias in reported volumetric water content. The bias is
mechanical in origin. It is not caused by firmware, and no firmware revision
corrects it.
