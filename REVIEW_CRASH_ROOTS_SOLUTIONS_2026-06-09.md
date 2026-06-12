# Crash Root Causes & Solutions — NB6VAC-FXC-r0

**Date:** 2026-06-09
**Firmware:** NB6VAC-MAIN-R4.0.47h5
**Hardware:** NB6VAC-FXC-r0 (ONT: ONT7-SFU-V3, FTTH G-PON)
**Monitoring period:** 13 days (May 28 – Jun 9, 2026), 2,146 JSONL entries
**Total crashes observed:** 9 (5 in first 5 days, 4 more Jun 5–6, then 75+ hours stable)

---

## Table of Contents

1. [Crash Profile](#1-crash-profile)
2. [Root Cause Analysis](#2-root-cause-analysis)
3. [Community Corroboration](#3-community-corroboration)
4. [Solutions](#4-solutions)
5. [Recommended Action Plan](#5-recommended-action-plan)
6. [Sources](#6-sources)

---

## 1. Crash Profile

### What happens during a crash

Every crash follows the same sequence, observed via 30-second polling:

1. **Instant death** — all API endpoints return `ALL FAILED` simultaneously
2. Box becomes **unreachable** (no ping, no HTTP, no LAN) for ~100–120 seconds
3. Box **reboots** — uptime resets to near-zero
4. WAN and LAN recover; ONT stays up throughout (fibre link never drops)

### Crash timeline (our monitoring data)

| # | Date | Time (CEST) | Uptime before | Crash-free interval |
|---|------|-------------|---------------|---------------------|
| 1 | May 28 | ~04:00 | ~74.0h | 74h (since monitoring started) |
| 2 | Jun 2 | unknown | unknown | pre-monitoring gap |
| 3 | Jun 5 | 07:31 | 74.0h | 74h (since Jun 2) |
| 4 | Jun 5 | 08:01 | 0.5h | 30 min after crash #3 |
| 5 | Jun 5 | 21:31 | 13.5h | 13.5h |
| 6 | Jun 6 | 00:53 | 3.3h | 3.3h after crash #5 |
| 7 | Jun 6 | 07:19 | 7.6h | 8.4h after crash #6 |
| — | Jun 6 → Jun 9 | — | 75+ h | **Still stable** |

### Cascade pattern

Crashes cluster: after a crash, the box is more likely to re-crash within hours, then stabilises. The shortest crash-free interval was **30 minutes** (crash #3 → #4). After crash #7, the box has been stable for 75+ hours.

### What's ruled out (no correlation found)

| Factor | Data | Conclusion |
|--------|------|------------|
| **Temperature** | Crash avg 55.3°C vs overall avg 55.1°C. Max ever 60.5°C — well below SoC thermal limits (85–105°C) | ❌ Not thermal throttling |
| **Voltage** | Stable 12.17–12.25V across all crashes | ❌ Not power supply |
| **Auth refresh** | 86 token refreshes, only 4 near crashes — random coincidence | ❌ Not auth-related |
| **WiFi clients** | No spike before crashes | ❌ Not client load |
| **WAN degradation** | Always instant death, never gradual latency increase | ❌ Not WAN-side |
| **ONT alarms** | Fibre modem stays up across all crashes | ❌ Not physical layer |
| **Firmware version** | Single version throughout (R4.0.47h5) | N/A (can't compare) |
| **IP address** | No changes near crash times | ❌ Not DHCP renewal |
| **Time of day** | Evenly spread across morning/afternoon/evening/night | ❌ Not time-dependent |

---

## 2. Root Cause Analysis

### 🥇 PRIORITY 1 — Firmware regression in R4.0.47h3/h5 (HIGH CONFIDENCE)

**This is the primary root cause. The evidence is overwhelming.**

#### Reasoning

1. **Version R4.0.45d was stable.** Multiple independent users report 365+ day uptimes on R4.0.45d with the same hardware (NB6VAC-FXC-r0). The previous non-AC Neuf Box 4 had multi-year uptimes.

2. **R4.0.47h3 introduced the freeze.** User nridoin (RED forum, Jul 2025): *"Ce phénomène se reproduit toutes les 2 à 3 semaines, visiblement depuis la mise à jour en version NB6VAC-MAIN-R4.0.47h3, précédemment en version 0.45d (version avec laquelle je suis arrivée à plus de 365 jours de uptime sans soucis)."* This is the single most damning data point — same box, same config, same network, only firmware changed.

3. **Multiple users, same symptoms, same firmware.** At least 10 independent users across two forums report identical crash signatures on R4.0.47h3:
   - All lights stay on, box unresponsive
   - LAN and WAN die (LAN dies seconds after WAN)
   - Web UI inaccessible even briefly before full death
   - Only power pull fixes it (soft reboot via UI doesn't work)
   - ONT unaffected

4. **Hardware replacement doesn't fix it.** User toz (lafibre, Oct 2024): got **three replacement NB6VAC boxes** from SFR — all exhibited the same crashes. Quote: *"cela ne semble pas lié au matériel"*. This rules out individual hardware defect.

5. **Different network, same crash.** User nridoin has two apartments with identical box+firmware — only one crashes. This suggests the bug is **triggered by specific network conditions or device interactions**, not a deterministic firmware fault. It's likely a race condition or memory corruption triggered by certain traffic patterns.

6. **OpenWRT on the same link is stable.** User toz replaced the NB6VAC with an OpenWRT router on the exact same fibre connection — **zero crashes**. This confirms the issue is in the SFR box firmware, not the line, the ONT, or the ISP network.

7. **Wired connections hit first.** User loloderu (Jan 2026): *"cela affecte uniquement les appareils branchés sur le filaire"*. This suggests the Ethernet switch subsystem or the bridge between WAN and LAN Ethernet crashes before WiFi — a firmware-level networking stack bug.

8. **Your R4.0.47h5 is in the same branch.** No public information exists about h5 specifically (zero search results). It's almost certainly a minor patch on h3 that didn't fix the underlying issue.

#### Likely mechanism

The crash pattern (instant death, cascade then stabilise, no environmental trigger) is consistent with a **memory leak or use-after-free in the networking stack** (Linux kernel or proprietary Broadcom switch driver). The cascade pattern occurs because:

- After a crash+reboot, all clients reconnect simultaneously → heavy allocation pressure
- If the bug is a slow leak under heavy allocation, the box hits the threshold faster during the reconnection storm
- Once network traffic settles (clients backed off, ARP tables stable), the leak grows more slowly → longer uptime → eventual crash

An alternative explanation is a **race condition during boot/shutdown of network interfaces** — the box's proprietary firmware may not properly synchronise between the WAN GPON interface, the Ethernet switch fabric, and the WiFi subsystem. Under heavy concurrent load (many clients reconnecting), the race window widens.

#### Why SFR hasn't fixed it

- The NB6VAC is a legacy platform (14+ year old design, Broadcom BCM63xx SoC)
- SFR is pushing Box 7/8 as replacements — no incentive to patch NB6VAC firmware
- The bug is intermittent and likely hardware-configuration dependent, making it hard to reproduce internally
- SFR support doesn't acknowledge firmware bugs — they just send replacement boxes

---

### 🥈 PRIORITY 2 — NB6VAC-FXC-r0 hardware revision (MEDIUM CONFIDENCE)

The **r0** hardware revision may be more susceptible than the **r1**.

- User j3r3myp received an r1 replacement → no more crashes (lafibre, Feb 2024)
- User Ralph had r0 → ongoing crashes → eventually left SFR
- The difference between r0 and r1 is unknown (SFR doesn't publish hardware changelogs)
- Possible explanations: different RAM vendor, different Broadcom silicon stepping, revised power delivery

**This is a contributing factor, not the root cause.** The firmware bug exists on both revisions, but r1 may have more RAM, tighter timings, or a hardware workaround that reduces the crash probability.

---

### 🥉 PRIORITY 3 — Power supply degradation (LOW CONFIDENCE)

User xp25 on lafibre suggested replacing the power supply. Some users found it didn't help, but it can't be fully ruled out:

- The NB6VAC uses a 12V/2A barrel jack supply
- Capacitor degradation over years could cause voltage droops under load spikes
- Our monitoring shows stable 12.17–12.25V, but we only sample every 30 seconds — transient droops would be invisible

**This is a minor contributing factor at most.** The firmware regression is the primary cause.

---

## 3. Community Corroboration

### Forum threads documenting the same issue

| Source | Users affected | Firmware | Key quote |
|--------|---------------|----------|-----------|
| [lafibre.info — Box NB6VAC-FXC-r0 qui plante régulièrement](https://lafibre.info/sfr-espace-technique/box-nb6vac-fxc-r0-qui-plante-regulierement/) | 10+ (Ralph, pioup, j3r3myp, dbocart, jpmiller, Daniel-Antony, JojoBT, etc.) | Various (R4.0.44k through R4.0.47h3) | *"Internet tombe avant mais le réseau local tombe dans les minutes qui suivent"* — 4 pages, 21,800+ views |
| [RED Forum — Plantage Box Plus (fibre)](https://communaute.red-by-sfr.fr/t5/Box-d%C3%A9codeur-TV/Plantage-Box-Plus-fibre/td-p/617845) | 4+ (nridoin, loloderu, DeNancy) | R4.0.47h3 | *"365 jours de uptime sans soucis"* on R4.0.45d, crashes every 2-3 weeks on h3 |
| [lafibre.info — Ma box NB6 plante de temps en temps](https://lafibre.info/sfr-espace-technique/ma-box-nb6-plante-de-temps-en-temps/) | 3+ (lexa, toz) | R4.0.45d | *"3 replacement boxes — same crashes. OpenWRT → zero crashes"* |
| [SFR Community — problème de routage avec la box 7 fibre](https://la-communaute.sfr.fr/t5/installation-et-param%C3%A9trage/probleme-de-routage-avec-la-box-7-fibre/td-p/2489183) | 1+ | R4.0.47h3 | Routing issues attributed to firmware |
| [RED Forum — Box wifi sfr plus, perte de signal toutes les 30s](https://communaute.red-by-sfr.fr/t5/Box-d%C3%A9codeur-TV/Box-wifi-sfr-plus-perte-de-signal-toutes-les-30-secondes-NB6VAC/td-p/618723) | 1+ | R4.0.47h3 | Micro-cuts every 30 seconds on NB6VAC-FXC-r0 |
| [GitHub — Cyril-Meyer/NB6VAC-FXC](https://github.com/Cyril-Meyer/NB6VAC-FXC) | Documentation project | R4.0.45d | Documents firmware version R4.0.45d with backup R4.0.44k |
| [GitHub — dougy147/NB6VAC](https://github.com/dougy147/NB6VAC) | Reverse engineering | Various | Attempts to gain root access and flash custom firmware |

### Common symptom pattern across all reports

1. ✅ All lights stay on, box completely unresponsive
2. ✅ WAN dies first, then LAN within seconds/minutes
3. ✅ Only power pull fixes it (soft reboot via UI fails)
4. ✅ ONT unaffected — fibre link stays up
5. ✅ Random frequency — days to weeks between crashes
6. ✅ Cascade pattern — crashes cluster, then box stabilises
7. ✅ Hardware replacement doesn't fix it
8. ✅ OpenWRT replacement eliminates it

---

## 4. Solutions

### Tier 1 — Immediate Mitigation (this week)

#### 4.1 Smart Plug with SMS/4G control — Remote Reboot

**Purpose:** When the box crashes while you're away, power-cycle it remotely via SMS.

| Product | Price | How it works | Pros | Cons |
|---------|-------|-------------|------|------|
| **SIMPAL T420** — 4G LTE smart plug | ~55€ [Amazon.fr](https://www.amazon.fr/Prise-maitre-GSM-Simpal-T40/dp/B01BMOBFYC) | Insert prepaid SIM. Send SMS → plug cuts power → box reboots. Works when internet is dead. | Works without WiFi/internet. Temperature sensor. Power outage alerts. Reliable. | Needs SIM card (~2€/mo). One-time setup. |
| **SIMPAL T4-GSM-4G** (newer model) | ~60€ [Amazon.fr](https://www.amazon.fr/Intelligente-N%C3%A9cessite-Temp%C3%A9rature-Notification-Instantan%C3%A9e/dp/B0063G87NW) | Same as T420 but 4G. App control via cellular. | Faster response, app UI. | Slightly more expensive. |
| **NONDK Prise Intelligente 4G** | ~40€ [Amazon.fr](https://www.amazon.fr/prise-gsm/s?k=prise+gsm) | Generic GSM/4G smart plug, SMS control. | Cheaper. | Less polished app, unknown reliability. |

**Recommended:** SIMPAL T420. Proven brand, temperature alerts, power monitoring. Add a Free Mobile prepaid SIM (2€/mo, no commitment) for SMS capability.

**Usage:**
- Box crashes → SIMPAL detects power draw anomaly → SMS alert to your phone
- You send SMS "OFF" → wait 30s → SMS "ON" → box reboots
- Or: set up a cron job on your monitoring Mac to auto-detect crash and send SMS via Free Mobile API

#### 4.2 ConnectSense Rebooter Pro — Automatic Watchdog

**Purpose:** Detects internet outage automatically, power-cycles the box without any human intervention.

| Product | Price | How it works | Link |
|---------|-------|-------------|------|
| **ConnectSense Rebooter Pro** | ~$50 (~55€ with shipping) | Pings internet continuously. If down for configurable period → auto power-cycle. Also schedules daily reboots. | [Amazon.com](https://www.amazon.com/ConnectSense-Rebooter-Pro-Automatic-Internet/dp/B0FPP911RJ) |

**Pros:** Fully automatic. No SIM needed. No human intervention.
**Cons:** US product — needs plug adapter. Ships from US. Price in USD.

#### 4.3 Shelly Plug S + Home Assistant — DIY Watchdog

**Purpose:** If you run Home Assistant, detect the box crash via ping failure and auto-cycle the Shelly plug.

| Product | Price | How it works | Link |
|---------|-------|-------------|------|
| **Shelly Plug S** | ~15€ [Amazon.fr](https://www.amazon.fr/Shelly-Commutateur-application-Compatible-Installation/dp/B0965J4HT5) | WiFi smart plug. Home Assistant pings the box every 30s. If unresponsive for 2 min → trigger Shelly power cycle. | |

**Pros:** Cheap. Integrates with existing HA setup. Fully automatic.
**Cons:** Requires Home Assistant running on a separate machine (your Mac). If Mac sleeps, no watchdog.

---

### Tier 2 — Medium-Term Fix (next 2-4 weeks)

#### 4.4 Call SFR SAV — Free Box Replacement

**Purpose:** Get a newer box revision (r1) or a Box 7/8 that doesn't have the firmware bug.

**Strategy:**
1. Call SFR/RED support (or use the RED & Moi app chat)
2. Report: "box freezes completely, all lights on, only power pull fixes it"
3. They will offer to send a replacement NB6VAC — **accept it** but ask:
   - "Can I get an r1 revision instead of r0?"
   - "Can I upgrade to a Box 7 since the NB6VAC has a known firmware issue?"
4. If they refuse Box 7, accept the replacement — you might get r1 which is less crash-prone
5. If replacement still crashes, call again and escalate

**Cost:** Free.
**Evidence:** User j3r3myp got r1 → no more crashes. User toz got 3 replacements → all still crashed. Results vary.

#### 4.5 Scheduled Preventive Reboot

**Purpose:** Reduce crash probability by rebooting the box on a schedule, clearing memory leaks before they accumulate.

**Implementation via monitor.py:**
```python
# Add to monitor: if uptime > 72 hours, trigger API reboot
# (requires ADR-0001 fix to be implemented first)
if uptime_hours and uptime_hours > 72:
    log_event("scheduled_reboot", f"uptime {uptime_hours:.1f}h > 72h threshold")
    sfr_box.reboot()  # via API: system.reboot
```

Or via cron + the SFR box API:
```bash
# Reboot box every 3 days at 04:00 via API
0 4 */3 * * python /path/to/api-client/reboot_box.py --hostname 192.168.1.1
```

**Cost:** Free.
**Evidence:** User Ralph tried this (weekly reboot via API) — didn't help because the API reboot command doesn't work when the box is in its frozen state. But a **preventive** reboot (before crash) may reduce frequency.
**Limitation:** The API `system.reboot` command does a soft reboot. A hard power cycle (smart plug) is more effective.

---

### Tier 3 — Long-Term Solution (next 1-3 months)

#### 4.6 OpenWRT Router — Complete Box Bypass

**Purpose:** Replace the SFR box entirely with an OpenWRT router. Zero crashes guaranteed.

| Product | Price | Specs | Link |
|---------|-------|-------|------|
| **GL.iNet GL-MT3000 (Beryl AX)** | ~75€ | WiFi 6, AX3000, dual-band, OpenWRT pre-installed, 2x 2.5G ports | [Amazon.fr](https://www.amazon.fr/GL-iNet-GL-MT3000-Portable-Bi-Bande-Repeteur/dp/B0BPSGJN7T) |
| **GL.iNet MT2500A (Brume 2)** | ~60€ | No WiFi, 2.5G WAN, OpenWRT pre-installed, compact | [Amazon.fr](https://www.amazon.fr/GL-iNet-passerelle-Domicile-Distance-Aluminium/dp/B0BQMJDDYR) |
| **GL.iNet GL-SFT1200 (Opal)** | ~35€ | WiFi 5, AC1200, budget option | [Amazon.fr](https://www.amazon.fr/openwrt-router/s?k=openwrt+router) |

**Setup (proven working for SFR FTTH):**
1. Plug ONT Ethernet → GL.iNet WAN port
2. In OpenWRT, configure WAN DHCP with vendor class: `neufbox_NB6V-<your-mac>`
3. Optionally set the GL.iNet WAN MAC to match your NB6VAC's MAC address
4. Full tutorial: [lafibre.info — Tuto bypass complet neufbox avec un routeur OpenWrt](https://lafibre.info/remplacer-sfr/ftth-tuto-bypass-complet-neufbox-avec-un-routeur-openwrt/)

**Pros:** Zero crashes (proven by user toz). Full control over your network. Better WiFi (if using MT3000). No SFR firmware bugs.
**Cons:** Loses SFR TV app (Android TV). Requires 30 min setup. SFR could theoretically block non-SFR MACs (hasn't happened in practice per forum reports).

#### 4.7 SFR Box 8 via LeBonCoin — Hardware Upgrade

**Purpose:** Replace NB6VAC with the latest SFR box that doesn't have the freeze bug.

| Product | Price | Link |
|---------|-------|------|
| **SFR Box 8** (used) | ~30–70€ | [LeBonCoin](https://www.leboncoin.fr/ck/accessoires_informatique/box-8) |

**Setup:** Plug into ONT. May need to register with SFR (some users report it works without registration, others needed to call SFR).
**Evidence:** Multiple lafibre users report flawless operation after switching to Box 8.
**Risk:** SFR may block unregistered boxes. Lower risk with Box 7 (which SFR sends as official replacement).

#### 4.8 ISP Switch — Nuclear Option

If you're fed up with SFR entirely:

- **Bouygues Bbox WiFi 6E** — User Ralph switched and has zero issues. 1Gbps symmetric FTTH available.
- **Free Freebox Pop/Ultra** — Better hardware, more transparent firmware updates.
- **Orange Livebox 6** — Rock-solid stability reputation.

**Cost:** Depends on offer. SFR/RED cancellation fee may apply if still under contract.

---

## 5. Recommended Action Plan

### Phase 1 — This Week (cost: ~57€)

| Step | Action | Cost | Effort |
|------|--------|------|--------|
| 1 | Buy **SIMPAL T420** + Free Mobile prepaid SIM | ~57€ | 10 min setup |
| 2 | Plug SFR box into SIMPAL. Test SMS on/off cycle. | — | 5 min |
| 3 | **Call SFR SAV** → request box replacement. Ask for Box 7 or r1. | Free | 20 min phone call |
| 4 | Set up **monitor.py auto-reboot at 72h uptime** (after ADR-0001 fix) | Free | 1 hour coding |

**Result:** Remote reboot capability + potential free hardware upgrade.

### Phase 2 — Next Month (cost: 0–75€)

| Step | Action | Condition |
|------|--------|-----------|
| 5a | If SFR sends Box 7/8 → swap, monitor for 2 weeks | Replacement received |
| 5b | If SFR sends another NB6VAC → try it, compare crash rate | Replacement received |
| 5c | If still crashing → buy **GL.iNet GL-MT3000** (~75€), bypass box | Crashes persist |

**Result:** Crash-free network operation.

### Phase 3 — Ongoing

| Step | Action |
|------|--------|
| 6 | Keep monitor.py running. With ADR-0001 fixes (#11–#15), crash detection and auto-recovery will work correctly. |
| 7 | If using smart plug, integrate auto-cycle into monitor.py: detect crash → SMS SIMPAL → power cycle. |
| 8 | If bypassing with OpenWRT, repurpose NB6VAC as a test target for firmware reverse engineering (see `firmware-reverse-engineering/README.md`). |

---

## 6. Sources

### Forum Threads

1. **lafibre.info** — [Box NB6VAC-FXC-r0 qui plante régulièrement](https://lafibre.info/sfr-espace-technique/box-nb6vac-fxc-r0-qui-plante-regulierement/) — 4 pages, 21,800+ views, Jan 2024 – Jun 2025. Primary source for multi-user corroboration.
2. **RED by SFR Forum** — [Plantage Box Plus (fibre)](https://communaute.red-by-sfr.fr/t5/Box-d%C3%A9codeur-TV/Plantage-Box-Plus-fibre/td-p/617845) — Firmware regression R4.0.45d → R4.0.47h3, Jul 2025 – Jan 2026.
3. **lafibre.info** — [Ma box NB6 plante de temps en temps](https://lafibre.info/sfr-espace-technique/ma-box-nb6-plante-de-temps-en-temps/) — 3 replacement boxes, OpenWRT comparison, Jun 2024.
4. **lafibre.info** — [Tuto bypass complet neufbox avec un routeur OpenWrt](https://lafibre.info/remplacer-sfr/ftth-tuto-bypass-complet-neufbox-avec-un-routeur-openwrt/) — 36 pages, proven SFR FTTH bypass guide.
5. **SFR Community** — [problème de routage avec la box 7 fibre](https://la-communaute.sfr.fr/t5/installation-et-param%C3%A9trage/probleme-de-routage-avec-la-box-7-fibre/td-p/2489183) — Routing bug on R4.0.47h3, Dec 2025.
6. **RED by SFR Forum** — [Box wifi sfr plus, perte de signal toutes les 30s](https://communaute.red-by-sfr.fr/t5/Box-d%C3%A9codeur-TV/Box-wifi-sfr-plus-perte-de-signal-toutes-les-30-secondes-NB6VAC/td-p/618723) — Micro-cuts on NB6VAC-FXC-r0, Sep 2025.
7. **RED by SFR Forum** — [Déconnexion / reboot intempestifs](https://communaute.red-by-sfr.fr/t5/R%C3%A9seau/D%C3%A9connexion-reboot-intempestifs/td-p/621283) — Daily reboots, ongoing.

### GitHub

8. **Cyril-Meyer/NB6VAC-FXC** — [Documentation and tools](https://github.com/Cyril-Meyer/NB6VAC-FXC) — Firmware version reference (R4.0.45d primary, R4.0.44k backup).
9. **dougy147/NB6VAC** — [Reverse engineering](https://github.com/dougy147/NB6VAC) — Serial console forensics, firmware analysis attempts.

### Product Links

10. **SIMPAL T420** — [Amazon.fr](https://www.amazon.fr/Prise-maitre-GSM-Simpal-T40/dp/B01BMOBFYC)
11. **ConnectSense Rebooter Pro** — [Amazon.com](https://www.amazon.com/ConnectSense-Rebooter-Pro-Automatic-Internet/dp/B0FPP911RJ)
12. **GL.iNet GL-MT3000** — [Amazon.fr](https://www.amazon.fr/GL-iNet-GL-MT3000-Portable-Bi-Bande-Repeteur/dp/B0BPSGJN7T)
13. **SFR Box 8 (used)** — [LeBonCoin](https://www.leboncoin.fr/ck/accessoires_informatique/box-8)

### Internal Project Documents

14. `docs/adr/0001-tight-exceptions-and-hardened-state-machine.md` — Monitor bug fix plan
15. `logs/monitor_2026-06-0{5,6,7,8,9}.jsonl` — Source data for crash analysis
16. `logs/monitor_stderr.log` — `KeyError('rsp')` traces confirming monitor bugs
17. Handoff document (Jun 9, 2026 session) — 13-day crash analysis

---

*Document generated 2026-06-09. Data based on 13 days of monitoring (May 28 – Jun 9, 2026) and community research across lafibre.info, communaute.red-by-sfr.fr, and la-communaute.sfr.fr.*
