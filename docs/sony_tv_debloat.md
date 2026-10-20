# Sony BRAVIA TV: inventory, debloat, and rooting notes

This runbook documents the living-room Sony BRAVIA XR-65X90K (2022 X90K
series, MediaTek MT5895, Google TV / Android 10). The model code is specific
to this unit — a different TV will report its own code, package set, and
launch activities, so verify against your own device before applying anything.
It is used in the voice-agent deployment as the `Fernseher` media surface and
is driven over ADB for app autostart. The goals here are:

1. Remove advertising, tracking, and unused vendor apps (debloat).
2. Keep a record of what is safe to disable and what must stay.
3. Document the rooting situation so it is not re-investigated from scratch.

All commands below are **reversible** and need **no root**. A factory reset
restores every package.

> **Firmware is deliberately frozen.** This TV stays on its current Android 10
> build, and firmware updates are actively blocked, until the rooting question
> is settled — see [Firmware: stay on the current version](#4-firmware-stay-on-the-current-version).
> Do **not** run a software update or accept an update prompt. Because a
> firmware update can also re-enable disabled packages, keeping updates off
> also keeps the debloat stable.

> This document contains no network addresses, hardware identifiers, or
> account data. Replace the `<tv-ip>` placeholder with the TV's address on
> your own network.

## 1. Connect over ADB

Enable **Developer options → USB debugging** and **Network debugging** on the
TV, then authorise the host key once (a dialog appears on the TV screen;
choose *Always allow from this computer*).

```bash
export TV=<tv-ip>:5555
adb connect "$TV"
adb -s "$TV" shell id          # expect uid=2000(shell)
```

The ADB port stays open across a cold boot and listens on all interfaces, so
restrict it at the network layer (firewall/VLAN) to the hosts that need it.

### Read the current state (read-only)

```bash
adb -s "$TV" shell getprop | grep -E 'ro.build.(fingerprint|version)'   # build, Android, patch level
adb -s "$TV" shell pm list packages -s                                  # system packages
adb -s "$TV" shell pm list packages -3                                  # user-installed packages
adb -s "$TV" shell pm list packages -d                                  # already-disabled packages
```

The on-screen firmware version is under **Help → software version**
(`PKG-xxxxxxxx`). ADB and the TV's local REST API do not expose it.

## 2. How to disable, uninstall, and revert

Two reversible mechanisms, neither needs root:

| Action | Command | Revert |
|---|---|---|
| Disable (keeps the APK) | `adb -s "$TV" shell pm disable-user --user 0 <pkg>` | `adb -s "$TV" shell pm enable <pkg>` |
| Uninstall for the user (keeps the system APK) | `adb -s "$TV" shell pm uninstall -k --user 0 <pkg>` | `adb -s "$TV" shell cmd package install-existing <pkg>` |

Prefer **disable** for system apps and **uninstall --user 0** for
side-loaded user apps. Always disable one package at a time and check the TV
still behaves before moving on. After a firmware update, re-check the list —
an update can silently re-enable packages.

A helper to disable a list:

```bash
for pkg in <pkg1> <pkg2> <pkg3>; do
  echo "disabling $pkg"
  adb -s "$TV" shell pm disable-user --user 0 "$pkg"
done
```

## 3. Debloat plan

### Stage 1 — advertising, tracking, demo (low risk)

Safe to disable for anyone; none of these are needed for normal TV, casting,
or the voice-agent integration.

| Package | What it is |
|---|---|
| `tv.samba.ssm` | Samba TV — automatic content recognition / ad tracking, runs as a foreground service |
| `com.sony.dtv.sonyselect` | Sony Select — promoted-app recommendations |
| `com.sony.dtv.sonytab` | The "Sony" tab/row on the home screen |
| `com.sonyericsson.idd.agent` | Sony usage-data upload agent (APK part) |
| `com.google.android.play.games` | Play Games (unused on this TV) |
| `de.hdplus.tvapp.androidtv` | HD+ promo app (cable TV is used instead) |
| `com.sony.dtv.demosupport` | In-store demo support |
| `com.sony.dtv.demosystemsupport` | In-store demo support |
| `com.sony.dtv.multiscreendemo` | In-store demo |

Uninstall the old side-loaded YouTube client as a **security** item — it uses
the legacy SmartTube application id whose signing key was leaked in late 2025,
so any package signed with that key would be accepted as a valid update:

```bash
adb -s "$TV" shell pm uninstall -k --user 0 com.teamsmart.videomanager.tv
```

### Stage 2 — unused apps and features (decide per household)

Disable only what you do not use. Each is self-contained.

| Package(s) | Feature | Disable if… |
|---|---|---|
| `com.sonypicturescore` | BRAVIA CORE (Sony's film store) | you never use it |
| `com.apple.atve.sony.appletv` | Apple TV app | you stream Apple TV elsewhere |
| `com.google.android.videos` | Google TV (store) app | you do not buy/rent here |
| `com.sony.dtv.sensingcamera.settings`, `com.sony.dtv.sensingcamera.fwupdate`, `com.sony.dtv.gestureapp`, `com.sony.dtv.distancealert`, `com.sony.dtv.sensingfusionservice` | BRAVIA CAM features | **no camera is attached — safe to disable** |
| `com.sony.dtv.seeds.iot`, `com.sony.dtv.seeds.iot.remotestart`, `com.sony.dtv.seeds.auth` | BRAVIA Connect cloud / remote start / Alexa | you do not use Sony's cloud or voice-remote-start |
| `com.sony.dtv.livingfit` | "Living decor" / art mode | unused |
| `com.google.android.apps.tv.dreamx` | Ambient-mode screensaver | you prefer the screen to sleep (set a different screensaver first) |
| `com.sony.dtv.ardlauncher`, `com.sony.dtv.iplayerlauncher`, `com.sony.dtv.itvlauncher`, `com.sony.dtv.my5launcher`, `com.sony.dtv.all4launcher`, `com.sony.dtv.soundslauncher`, `com.youview.poa` | UK broadcaster placeholder launchers | not in the UK (several ship disabled already) |

> Remote-start note: disabling `seeds.*` removes Sony's own wake-on-cloud path.
> Wake-on-LAN and the Android TV remote service (used by the voice-agent
> integration) are independent of it.

### Stage 3 — test individually (side effects possible)

Disable one at a time and watch for regressions; re-enable if anything breaks.

| Package | Risk |
|---|---|
| `com.uei.quicksetsdk.sony` | IR control of external devices (soundbar/receiver) and device discovery |
| `com.sony.dtv.interactivetvutil` | Helper used by broadcast/Samba features; also feeds EPG/usage context |
| `com.sony.dtv.usercontext` | User/usage context provider |
| `com.qterics.da.product`, `com.sony.dtv.da.service` | Sony remote support / diagnostics |
| `com.sony.dtv.b2b.hotelmode`, `com.sony.dtv.b2b.rs232csupport`, `com.sony.dtv.b2b.prosettings` | Hotel / pro-install modes (unused at home, but low memory footprint) |
| `com.vewd.core.integration.dia`, `com.sony.dtv.hbbtvplayer` | Internet browser / HbbTV (red-button broadcast apps) |

### Keep — do not disable

These break setup, the home screen, casting, live TV, updates, voice search,
or the voice-agent integration.

| Package(s) | Why it must stay |
|---|---|
| `com.google.android.gms`, `com.google.android.gsf`, `com.android.vending` | Google Play services / framework / Store |
| `com.google.android.tungsten.setupwraith` | Device setup assistant |
| `com.google.android.apps.tv.launcherx` | The home launcher (do not disable without a replacement installed) |
| `com.google.android.tv.remote.service` | Android TV remote service — used by the Home Assistant integration |
| `com.google.android.apps.mediashell` | Chromecast built-in — used by Music Assistant / casting |
| `com.google.android.katniss` | Voice search (the TV has no built-in mic; this serves the remote's mic) |
| `com.sony.dtv.tvx`, `com.sony.dtv.tvinput.*`, `com.sony.dtv.tvapi` | Live TV / tuner / external inputs |
| `com.sony.dtv.airplayapp`, `com.sony.dtv.homekit` | AirPlay / HomeKit receiver — **kept: AirPlay to this TV is used occasionally** |
| `com.sony.dtv.recapp`, `com.sony.dtv.recservice`, `com.sony.dtv.hddmgr` | USB-HDD recording — **kept: recording to USB is in use** |
| `com.sony.dtv.update`, `com.sony.dtv.systemupdate`, `com.sony.dtv.otaurlprovisioning` | Firmware update chain — leave installed so updates can be re-enabled later; block the update *check* at the menu and network instead (see [Firmware](#4-firmware-stay-on-the-current-version)), do not delete these |
| `com.sony.dtv.webapi.core`, `com.sony.dtv.sonysystemservice`, `com.sony.dtv.sonyvendorservice` | Core system/vendor services (power, inputs, picture/audio) |

### Settings-menu changes (no ADB)

Some things are cleaner from the TV menu and complement the ADB steps:

- Switch the home screen to **Apps-only mode** to remove the promoted banners.
- Turn off **Samba Interactive TV** in the privacy settings.
- Review Sony's data/privacy consents.
- Turn off Google **Usage & diagnostics** and reset the advertising ID.
- Turn off per-app notifications for the vendor tip/ad channels.
- Turn off **HbbTV** if unused.

### Network-level blocking (recommended)

Some native telemetry daemons (`iddd`, `idds`, `idd-logreader`) are started by
`init`, not installed as APKs, so `pm disable` cannot touch them. Block their
destinations at the network instead — a DNS sinkhole (Pi-hole/AdGuard) or a
firewall rule on the TV's VLAN neutralises Samba TV and Sony/Google telemetry
regardless of which packages remain. This is the only way to stop the native
daemons without root, and it is fully reversible.

## 4. Firmware: stay on the current version

The TV is kept on its current Android 10 build on purpose. It is several Sony
releases behind, and the latest EU firmware would move it to Android 12, but we
do **not** update yet: a future root method may depend on a specific firmware
version, and updating could close that option for good or change the package
set. Revisit this only once there is concrete, device-specific rooting
information (see [Rooting](#5-rooting)).

**Do not** start an update from the menu, insert a USB stick with a firmware
file, or accept an on-screen update prompt. Block updates with these
reversible measures, strongest last:

1. **Menu:** Settings → System → About → System software update → turn
   **Automatic software update** off.
2. **Network (recommended):** block Sony's OTA/update hosts for the TV at the
   DNS sinkhole or VLAN firewall. This is the active, reliable block and is
   trivial to lift later. Fold it into the same blocklist used for telemetry
   (see [network-level blocking](#network-level-blocking-recommended)).
3. **Optional, stronger:** disable the update-checker apps over ADB. Keep the
   APKs (do not uninstall) so updates can be restored with `pm enable`:
   ```bash
   for pkg in com.sony.dtv.systemupdate com.sony.dtv.otaurlprovisioning; do
     adb -s "$TV" shell pm disable-user --user 0 "$pkg"
   done
   ```
   Leave `com.sony.dtv.update` enabled (it also handles unrelated app/session
   management). Re-enable both checkers before you ever intend to update.

To confirm nothing has changed later, re-check the on-screen version under
**Help → software version**; it must stay `PKG6.7612.xxxxEUA` (the build this
unit shipped on in this deployment).

## 5. Rooting

Findings from a read-only inspection of this unit. **Summary: there is no
practical, safe root path for this TV, and root would add little for the two
use cases it is usually wanted for (deeper debloat and an on-TV Hyperion
ambilight grabber).**

### Why root is not feasible here

Values read from the device:

| Property | Value | Meaning |
|---|---|---|
| ADB user | `uid=2000(shell)` | unprivileged shell only |
| `ro.build.type` / `ro.build.tags` | `user` / `release-keys` | production, Sony-signed build |
| `ro.debuggable` / `ro.secure` | `0` / `1` | `adb root` is refused ("cannot run as root in production builds") |
| `ro.boot.verifiedbootstate` | `green` | Verified Boot active |
| `ro.boot.vbmeta.device_state` | `locked` | bootloader locked |
| `ro.boot.veritymode` | `enforcing` | dm-verity blocks any system/vendor modification |
| `ro.oem_unlock_supported`, `sys.oem_unlock_allowed` | empty | no OEM-unlock path; the toggle is absent on BRAVIA |
| SELinux | `Enforcing` | — |
| `su` / Magisk | not present | — |

Magisk needs either an unlockable bootloader (to boot a patched image) or a
working exploit chain. Sony consumer TVs expose **no** `fastboot flashing
unlock`, unlike most phones or the NVIDIA Shield. The only theoretical route is
a MediaTek BootROM exploit, and for the MT5895 TV SoC there is no public,
reliable chain. A TV also has no on-screen download mode and no
consumer-available flash tool — service firmware is dealer-only — so a failed
attempt is a likely unrecoverable brick, unlike a phone where recovery is
usually possible.

### Why root would not help much anyway

- **Debloat:** every item in the three stages above works without root. Root
  would only add removing the native `idd*` daemons and deleting packages from
  `/system`, both better handled by the network-level block.
- **Hyperion ambilight:** an on-device grabber — even with root — cannot
  capture HDCP/DRM content; streaming apps return a black frame. This is a
  hardware/platform restriction, not a permissions one, so root does not solve
  it. The ambilight approach is a separate task and is deliberately left open
  here; it will be designed later, with this DRM limit as a known constraint.

### Verdict

No safe, public root method exists for this TV today. Keep it unrooted **for
now** and, importantly, stay on the current firmware with updates blocked so a
future version-specific method is not lost. In the meantime, debloat over ADB
and block telemetry and updates at the network. Revisit rooting only with
concrete, device-specific information for this exact model and firmware;
ambilight is handled as its own task afterwards.
