import json
import os
import re
import sys
import traceback
import uuid
from copy import deepcopy
from datetime import datetime
from time import time


LOG_PATH = "/tmp/pyfa-mcp.log"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-11-25"}
ENGINE_READY = False
TRANSPORT_MODE = None  # "headers" | "ndjson"

# Lazy-loaded symbols (initialized by _ensure_engine)
eos = None
calculateRangeFactor = None
FittingModuleState = None
FittingSlot = None
FittingHardpoint = None
Cargo = None
Character = None
Drone = None
Fit = None
Module = None
Ship = None
Citadel = None
SavedDamagePattern = None
SavedTargetProfile = None
SavedPriceStatus = None
pyfa_config = None


def _log(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as stream:
            stream.write(f"{datetime.utcnow().isoformat()}Z {message}\n")
    except Exception:
        # Avoid recursive logger failures impacting transport loop
        pass


def _ensure_engine():
    global ENGINE_READY
    global eos
    global pyfa_config
    global calculateRangeFactor
    global FittingModuleState, FittingSlot, FittingHardpoint
    global Cargo, Character, Drone, Fit, Module, Ship, Citadel
    global SavedDamagePattern, SavedTargetProfile
    global SavedPriceStatus

    if ENGINE_READY:
        return

    import config as config_pkg
    import eos as eos_pkg
    import eos.config  # noqa: F401
    import eos.db
    import eos.events  # noqa: F401
    from eos.calc import calculateRangeFactor as range_factor
    from eos.const import FittingHardpoint as fitting_hardpoint, FittingModuleState as module_state, FittingSlot as fitting_slot
    from eos.saveddata.cargo import Cargo as cargo_cls
    from eos.saveddata.character import Character as character_cls
    from eos.saveddata.citadel import Citadel as citadel_cls
    from eos.saveddata.damagePattern import DamagePattern as damage_pattern_cls
    from eos.saveddata.drone import Drone as drone_cls
    from eos.saveddata.fit import Fit as fit_cls
    from eos.saveddata.module import Module as module_cls
    from eos.saveddata.price import PriceStatus as price_status_cls
    from eos.saveddata.ship import Ship as ship_cls
    from eos.saveddata.targetProfile import TargetProfile as target_profile_cls

    pyfa_config = config_pkg
    eos = eos_pkg
    calculateRangeFactor = range_factor
    FittingModuleState = module_state
    FittingSlot = fitting_slot
    FittingHardpoint = fitting_hardpoint
    Cargo = cargo_cls
    Character = character_cls
    Citadel = citadel_cls
    SavedDamagePattern = damage_pattern_cls
    Drone = drone_cls
    Fit = fit_cls
    Module = module_cls
    SavedPriceStatus = price_status_cls
    Ship = ship_cls
    SavedTargetProfile = target_profile_cls

    if pyfa_config.savePath is None:
        pyfa_config.defPaths()
    if not os.path.exists(pyfa_config.savePath):
        os.mkdir(pyfa_config.savePath)
    eos.db.saveddata_meta.create_all()

    ENGINE_READY = True
    _log("engine initialized")


def _jsonrpc_ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _jsonrpc_err(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _read_message(stdin):
    global TRANSPORT_MODE
    reader = getattr(stdin, "buffer", stdin)
    headers = {}
    while True:
        line = reader.readline()
        if not line:
            return None
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")

        # Some MCP routers proxy stdio as newline-delimited JSON instead of
        # Content-Length framed payloads; auto-detect and support both.
        stripped = line.strip()
        if not headers and stripped.startswith("{"):
            TRANSPORT_MODE = "ndjson"
            _log("transport detected: ndjson")
            return json.loads(stripped)

        if line in ("\n", "\r\n"):
            break
        if ":" not in line:
            _log(f"malformed header line: {line!r}")
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()

    length = int(headers.get("content-length", "0"))
    if length <= 0:
        _log(f"missing/invalid content-length header: {headers}")
        return None
    TRANSPORT_MODE = "headers"
    payload = reader.read(length)
    if not payload:
        _log("read empty payload body")
        return None
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if len(payload) < length:
        _log(f"short payload read: expected={length} got={len(payload)}")
    return json.loads(payload.decode("utf-8"))


def _write_message(stdout, data):
    global TRANSPORT_MODE
    writer = getattr(stdout, "buffer", stdout)
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    if TRANSPORT_MODE == "ndjson":
        writer.write(payload + b"\n")
        writer.flush()
        return
    writer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8"))
    writer.write(payload)
    writer.flush()


def _coerce_json(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _coerce_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_coerce_json(v) for v in value]
    return str(value)


def _normalize_states(fit):
    for mod in fit.modules:
        mod.owner = fit
        if mod.isEmpty:
            continue
        can_have_state = mod.canHaveState(mod.state)
        if can_have_state is not True:
            mod.state = can_have_state
        elif not mod.isValidState(mod.state):
            mod.state = FittingModuleState.ONLINE


def _ensure_fit_ownership(fit):
    for mod in fit.modules:
        mod.owner = fit
    for mod in fit.projectedModules:
        mod.owner = fit
    for drone in fit.drones:
        drone.owner = fit
    for drone in fit.projectedDrones:
        drone.owner = fit
    for fighter in fit.fighters:
        fighter.owner = fit
    for fighter in fit.projectedFighters:
        fighter.owner = fit


def _recalculate_fit(fit):
    _ensure_fit_ownership(fit)
    fit.factorReload = eos.config.settings["useGlobalForceReload"]
    fit.clear()
    _normalize_states(fit)
    fit.calculateModifiedAttributes()


def _parse_header(text):
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            match = re.match(r"^\[(?P<ship>[^,\]]+)\s*,\s*(?P<name>[^\]]+)\]$", stripped)
            if match is None:
                return None, None
            return match.group("ship").strip(), match.group("name").strip()
    return None, None


def _import_eft_text(text, character_profile="all5"):
    _ensure_engine()
    ship_name, fit_name = _parse_header(text)
    if not ship_name:
        raise ValueError("fit header is missing or invalid")

    ship_item = eos.db.getItem(ship_name, eager=("attributes", "group.category"))
    if ship_item is None:
        raise ValueError(f"unknown ship: {ship_name}")

    fit = Fit(ship=Ship(ship_item), name=fit_name or "Imported Fit")
    if character_profile == "all0":
        fit.character = Character.getAll0()
    else:
        fit.character = Character.getAll5()

    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            continue

        amount_match = re.match(r"^(?P<name>.+?)\s+x(?P<amount>\d+)\s*$", line)
        if amount_match:
            item_name = amount_match.group("name").strip()
            amount = int(amount_match.group("amount"))
            item = eos.db.getItem(item_name, eager=("attributes", "group.category"))
            if item is None:
                continue
            category_name = item.group.category.name
            if category_name == "Drone":
                try:
                    drone = Drone(item)
                except ValueError:
                    continue
                drone.amount = amount
                drone.amountActive = amount
                fit.drones.append(drone)
            else:
                cargo = Cargo(item)
                cargo.amount = amount
                fit.cargo.append(cargo)
            continue

        offline = line.lower().endswith("/offline")
        if offline:
            line = line[:-8].strip()

        if "," in line:
            module_name, charge_name = [part.strip() for part in line.split(",", 1)]
        else:
            module_name, charge_name = line, None

        item = eos.db.getItem(module_name, eager=("attributes", "group.category"))
        if item is None:
            continue

        try:
            module = Module(item)
        except ValueError:
            continue

        module.owner = fit

        default_state = module.getMaxState(proposedState=FittingModuleState.ACTIVE)
        if default_state is not None:
            module.state = default_state

        if offline:
            module.state = FittingModuleState.OFFLINE

        if charge_name:
            charge_item = eos.db.getItem(charge_name, eager=("attributes",))
            if charge_item is not None:
                try:
                    module.charge = charge_item
                except ValueError:
                    pass

        fit.modules.append(module)

    fit.fill()
    return fit


def _export_eft_text(fit):
    lines = [f"[{fit.ship.item.typeName}, {fit.name}]", ""]

    slot_order = (
        FittingSlot.LOW.value,
        FittingSlot.MED.value,
        FittingSlot.HIGH.value,
        FittingSlot.RIG.value,
        FittingSlot.SUBSYSTEM.value,
        FittingSlot.SERVICE.value
    )
    for slot in slot_order:
        rack = [m for m in fit.modules if (not m.isEmpty and m.slot == slot)]
        for module in rack:
            line = module.item.typeName
            if module.charge is not None:
                line = f"{line}, {module.charge.typeName}"
            if module.state == FittingModuleState.OFFLINE:
                line = f"{line} /offline"
            lines.append(line)
        if rack:
            lines.append("")

    for drone in fit.drones:
        if drone.amount > 0:
            lines.append(f"{drone.item.typeName} x{drone.amount}")
    if fit.drones:
        lines.append("")

    for cargo in fit.cargo:
        if cargo.amount > 0:
            lines.append(f"{cargo.item.typeName} x{cargo.amount}")

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _fit_snapshot(fit, range_km=None, target_speed=0, target_sig=40, target_resists=None):
    _ensure_engine()
    _ensure_fit_ownership(fit)
    _recalculate_fit(fit)
    total_dps = fit.getTotalDps().total
    total_volley = fit.getTotalVolley().total
    ehp_total = sum(fit.ehp.values())
    tank = fit.effectiveTank
    sustainable = fit.effectiveSustainableTank

    if target_resists is None:
        target_resists = (0.0, 0.0, 0.0, 0.0)
    dps_at_range = _calc_applied_dps(
        fit=fit,
        distance_km=range_km,
        target_speed=target_speed,
        target_sig=target_sig,
        target_resists=target_resists)

    cpu_output = fit.ship.getModifiedItemAttr("cpuOutput")
    powergrid_output = fit.ship.getModifiedItemAttr("powerOutput")
    calibration_capacity = fit.ship.getModifiedItemAttr("upgradeCapacity")
    cpu_headroom = cpu_output - fit.cpuUsed
    powergrid_headroom = powergrid_output - fit.pgUsed
    calibration_headroom = calibration_capacity - fit.calibrationUsed
    fit_valid = cpu_headroom >= 0 and powergrid_headroom >= 0 and calibration_headroom >= 0

    return {
        "name": fit.name,
        "ship": fit.ship.item.typeName,
        "dps": total_dps,
        "volley": total_volley,
        "dps_at_range": dps_at_range,
        "ehp": ehp_total,
        "tank": {
            "shield": tank.get("shieldRepair", 0),
            "armor": tank.get("armorRepair", 0),
            "hull": tank.get("hullRepair", 0),
            "passive_shield": tank.get("passiveShield", 0),
            "total": sum(tank.values())
        },
        "sustainable_tank": {
            "shield": sustainable.get("shieldRepair", 0),
            "armor": sustainable.get("armorRepair", 0),
            "hull": sustainable.get("hullRepair", 0),
            "passive_shield": sustainable.get("passiveShield", 0),
            "total": sum(sustainable.values())
        },
        "capacitor": {
            "stable": bool(fit.capStable),
            "state": fit.capState,
            "used_per_second": fit.capUsed,
            "recharge_per_second": fit.capRecharge
        },
        "mobility": {
            "max_speed": fit.maxSpeed,
            "align_time": fit.alignTime,
            "warp_speed": fit.warpSpeed,
            "max_target_range": fit.maxTargetRange,
            "scan_strength": fit.scanStrength
        },
        "resources": {
            "cpu_used": fit.cpuUsed,
            "cpu_output": cpu_output,
            "cpu_headroom": cpu_headroom,
            "powergrid_used": fit.pgUsed,
            "powergrid_output": powergrid_output,
            "powergrid_headroom": powergrid_headroom,
            "calibration_used": fit.calibrationUsed,
            "calibration_capacity": calibration_capacity,
            "calibration_headroom": calibration_headroom
        },
        "fit_valid": fit_valid
    }

def _calc_applied_dps(fit, distance_km, target_speed, target_sig, target_resists):
    if distance_km is None:
        distance_m = None
    else:
        distance_m = max(0.0, float(distance_km) * 1000.0)

    def get_distance_factor(item):
        if distance_m is None:
            return 1.0

        missile_data = getattr(item, "missileMaxRangeData", None)
        if missile_data is not None:
            lower_range, higher_range, higher_chance = missile_data
            if distance_m <= lower_range:
                return 1.0
            if distance_m <= higher_range:
                return max(0.0, min(1.0, float(higher_chance)))
            return 0.0

        optimal = getattr(item, "maxRange", None) or 0
        falloff = getattr(item, "falloff", None) or 0
        return max(0.0, min(1.0, calculateRangeFactor(optimal, falloff, distance_m)))

    def to_scalar_dps(value):
        return float(getattr(value, "total", value) or 0.0)

    ranged_dps = 0.0
    for mod in fit.activeModulesIter():
        if not mod.isDealingDamage():
            continue
        ranged_dps += to_scalar_dps(mod.getDps()) * get_distance_factor(mod)

    for drone in fit.activeDronesIter():
        if not drone.isDealingDamage():
            continue
        ranged_dps += to_scalar_dps(drone.getDps()) * get_distance_factor(drone)

    for fighter in fit.activeFightersIter():
        if not fighter.isDealingDamage():
            continue
        dps_per_effect = fighter.getDpsPerEffect()
        if dps_per_effect:
            ranged_dps += sum(to_scalar_dps(v) for v in dps_per_effect.values()) * get_distance_factor(fighter)

    resists = list(target_resists or (0.0, 0.0, 0.0, 0.0))[:4]
    while len(resists) < 4:
        resists.append(0.0)
    resists = [max(0.0, min(0.99, float(v))) for v in resists]
    average_resist = sum(resists) / 4.0
    resist_factor = 1.0 - average_resist

    return ranged_dps * resist_factor


def _dominates(a, b):
    maximize_keys = ("dps", "dps_at_range", "ehp", "tank_total", "sustainable_tank_total", "max_speed")
    minimize_keys = ("align_time",)

    a_better_or_equal = True
    a_strict_better = False

    for key in maximize_keys:
        if a[key] < b[key]:
            a_better_or_equal = False
            break
        if a[key] > b[key]:
            a_strict_better = True
    if a_better_or_equal:
        for key in minimize_keys:
            if a[key] > b[key]:
                a_better_or_equal = False
                break
            if a[key] < b[key]:
                a_strict_better = True
    return a_better_or_equal and a_strict_better


def _pareto_prune(candidates):
    kept = []
    for candidate in candidates:
        dominated = False
        for other in candidates:
            if other is candidate:
                continue
            if _dominates(other["score"], candidate["score"]):
                dominated = True
                break
        if not dominated:
            kept.append(candidate)
    return kept


def _score_from_snapshot(snapshot):
    return {
        "dps": snapshot["dps"],
        "dps_at_range": snapshot["dps_at_range"],
        "ehp": snapshot["ehp"],
        "tank_total": snapshot["tank"]["total"],
        "sustainable_tank_total": snapshot["sustainable_tank"]["total"],
        "max_speed": snapshot["mobility"]["max_speed"],
        "align_time": snapshot["mobility"]["align_time"]
    }


def _slot_label(slot_value):
    slot_labels = {
        FittingSlot.LOW.value: "low",
        FittingSlot.MED.value: "mid",
        FittingSlot.HIGH.value: "high",
        FittingSlot.RIG.value: "rig",
        FittingSlot.SUBSYSTEM.value: "subsystem",
        FittingSlot.SERVICE.value: "service",
        FittingSlot.SYSTEM.value: "system"
    }
    return slot_labels.get(slot_value, str(slot_value))


def _fit_module_listing(fit):
    modules = []
    for idx, mod in enumerate(fit.modules):
        modules.append({
            "slot_index": idx,
            "slot": _slot_label(mod.slot),
            "is_empty": bool(mod.isEmpty),
            "module_name": None if mod.isEmpty else mod.item.typeName,
            "charge_name": None if mod.isEmpty or mod.charge is None else mod.charge.typeName,
            "state": None if mod.isEmpty else int(mod.state)
        })
    return modules


def _replace_module_in_fit(fit, slot_index, module_ref=None, charge_ref=None, state=None):
    if not isinstance(slot_index, int):
        raise ValueError("slot_index must be an integer")
    if slot_index < 0 or slot_index >= len(fit.modules):
        raise ValueError("slot_index is out of bounds")

    current = fit.modules[slot_index]
    target_slot = current.slot

    if module_ref in (None, "", "empty"):
        fit.modules[slot_index] = Module.buildEmpty(target_slot)
        return

    item = eos.db.getItem(module_ref, eager=("attributes", "group.category"))
    if item is None:
        raise ValueError(f"unknown module: {module_ref}")

    new_module = Module(item)
    if new_module.slot != target_slot:
        raise ValueError("module slot does not match target slot")
    new_module.owner = fit

    fit.modules[slot_index] = new_module

    if not new_module.fits(fit):
        raise ValueError("module cannot be fitted in this slot on current hull")

    if charge_ref not in (None, ""):
        charge_item = eos.db.getItem(charge_ref, eager=("attributes",))
        if charge_item is None:
            raise ValueError(f"unknown charge: {charge_ref}")
        if not new_module.isValidCharge(charge_item):
            raise ValueError("charge is invalid for this module")
        new_module.charge = charge_item

    if state is None:
        desired_state = new_module.getMaxState(proposedState=FittingModuleState.ACTIVE)
        if desired_state is not None:
            new_module.state = desired_state
    else:
        desired_state = int(state)
        if not new_module.isValidState(desired_state):
            raise ValueError("requested module state is invalid")
        new_module.state = desired_state


def _search_slot_candidates(fit, slot_index, query=None, limit=24, include_variations=True):
    if slot_index < 0 or slot_index >= len(fit.modules):
        raise ValueError("slot_index is out of bounds")
    if limit <= 0:
        return []

    current = fit.modules[slot_index]
    target_slot = current.slot
    seen = set()
    candidates = []

    def add_item(item):
        if item is None:
            return
        if item.ID in seen:
            return
        if Module.calculateSlot(item) != target_slot:
            return
        seen.add(item.ID)
        candidates.append(item)

    if include_variations and not current.isEmpty:
        vars_ = eos.db.getVariations([current.item.ID], groupIDs=[current.item.groupID], eager=("attributes", "group.category"))
        for item in vars_:
            add_item(item)

    if query:
        found = eos.db.searchItems(query, eager=("attributes", "group.category"))
        for item in found:
            add_item(item)

    if not candidates and not current.isEmpty:
        add_item(current.item)

    return candidates[:limit]


def _objective_value(snapshot, objective):
    objective_map = {
        "dps": lambda s: s["dps"],
        "dps_at_range": lambda s: s["dps_at_range"],
        "ehp": lambda s: s["ehp"],
        "tank": lambda s: s["tank"]["total"],
        "sustainable_tank": lambda s: s["sustainable_tank"]["total"],
        "speed": lambda s: s["mobility"]["max_speed"],
        "align_time": lambda s: s["mobility"]["align_time"]
    }
    getter = objective_map.get(objective)
    if getter is None:
        raise ValueError("unsupported objective")
    return float(getter(snapshot))


def _hull_slot_summary(hull_ref, character_profile="all5"):
    _ensure_engine()

    hull_item = eos.db.getItem(hull_ref, eager=("attributes", "group.category"))
    if hull_item is None:
        raise ValueError(f"unknown hull: {hull_ref}")

    try:
        hull = Ship(hull_item)
    except ValueError:
        hull = Citadel(hull_item)

    fit = Fit(ship=hull, name=f"{hull_item.typeName} Slot Summary")
    if character_profile == "all0":
        fit.character = Character.getAll0()
    else:
        fit.character = Character.getAll5()

    fit.fill()
    _recalculate_fit(fit)

    def slot_data(slot):
        return {
            "total": int(fit.getNumSlots(slot) or 0),
            "used": int(fit.getSlotsUsed(slot) or 0),
            "free": int(fit.getSlotsFree(slot) or 0)
        }

    turret_total = int(fit.ship.getModifiedItemAttr("turretSlotsLeft") or 0)
    launcher_total = int(fit.ship.getModifiedItemAttr("launcherSlotsLeft") or 0)
    calibration_capacity = float(fit.ship.getModifiedItemAttr("upgradeCapacity") or 0)
    calibration_used = float(fit.calibrationUsed or 0)

    return {
        "hull": hull_item.typeName,
        "hull_id": hull_item.ID,
        "character_profile": character_profile,
        "slots": {
            "low": slot_data(FittingSlot.LOW),
            "mid": slot_data(FittingSlot.MED),
            "high": slot_data(FittingSlot.HIGH),
            "rig": slot_data(FittingSlot.RIG),
            "subsystem": slot_data(FittingSlot.SUBSYSTEM),
            "service": slot_data(FittingSlot.SERVICE),
            "fighter_light": slot_data(FittingSlot.F_LIGHT),
            "fighter_support": slot_data(FittingSlot.F_SUPPORT),
            "fighter_heavy": slot_data(FittingSlot.F_HEAVY),
            "fighter_standup_light": slot_data(FittingSlot.FS_LIGHT),
            "fighter_standup_support": slot_data(FittingSlot.FS_SUPPORT),
            "fighter_standup_heavy": slot_data(FittingSlot.FS_HEAVY)
        },
        "hardpoints": {
            "turret": {
                "total": turret_total,
                "used": int(fit.getHardpointsUsed(FittingHardpoint.TURRET) or 0),
                "free": int(fit.getHardpointsFree(FittingHardpoint.TURRET) or 0)
            },
            "launcher": {
                "total": launcher_total,
                "used": int(fit.getHardpointsUsed(FittingHardpoint.MISSILE) or 0),
                "free": int(fit.getHardpointsFree(FittingHardpoint.MISSILE) or 0)
            }
        },
        "calibration": {
            "capacity": calibration_capacity,
            "used": calibration_used,
            "free": calibration_capacity - calibration_used
        }
    }


def _resolve_damage_pattern_ref(pattern_ref):
    if pattern_ref in (None, "", "default"):
        return None

    pattern = eos.db.getDamagePattern(pattern_ref)
    if pattern is not None:
        return pattern

    if isinstance(pattern_ref, str):
        for builtin in SavedDamagePattern.getBuiltinList():
            if builtin.name == pattern_ref or builtin.rawName == pattern_ref:
                return builtin

    raise ValueError(f"unknown damage pattern: {pattern_ref}")


def _resolve_target_profile_ref(profile_ref):
    if profile_ref in (None, "", "default"):
        return None

    profile = eos.db.getTargetProfile(profile_ref)
    if profile is not None:
        return profile

    if isinstance(profile_ref, str):
        for builtin in SavedTargetProfile.getBuiltinList():
            if builtin.name == profile_ref or builtin.rawName == profile_ref:
                return builtin

    raise ValueError(f"unknown target profile: {profile_ref}")


def _validate_fit_explain(fit):
    _ensure_fit_ownership(fit)
    snapshot = _fit_snapshot(fit)
    issues = []

    resources = snapshot["resources"]
    if resources["cpu_headroom"] < 0:
        issues.append({
            "code": "cpu_exceeded",
            "message": "CPU usage exceeds hull output",
            "details": {
                "cpu_used": resources["cpu_used"],
                "cpu_output": resources["cpu_output"],
                "cpu_deficit": abs(resources["cpu_headroom"])
            }
        })
    if resources["powergrid_headroom"] < 0:
        issues.append({
            "code": "powergrid_exceeded",
            "message": "Powergrid usage exceeds hull output",
            "details": {
                "powergrid_used": resources["powergrid_used"],
                "powergrid_output": resources["powergrid_output"],
                "powergrid_deficit": abs(resources["powergrid_headroom"])
            }
        })
    if resources["calibration_headroom"] < 0:
        issues.append({
            "code": "calibration_exceeded",
            "message": "Calibration usage exceeds hull capacity",
            "details": {
                "calibration_used": resources["calibration_used"],
                "calibration_capacity": resources["calibration_capacity"],
                "calibration_deficit": abs(resources["calibration_headroom"])
            }
        })

    slot_labels = (
        ("low", FittingSlot.LOW),
        ("mid", FittingSlot.MED),
        ("high", FittingSlot.HIGH),
        ("rig", FittingSlot.RIG),
        ("subsystem", FittingSlot.SUBSYSTEM),
        ("service", FittingSlot.SERVICE)
    )
    for label, slot in slot_labels:
        free = fit.getSlotsFree(slot)
        if free < 0:
            issues.append({
                "code": "slots_exceeded",
                "message": f"{label} slots exceeded",
                "details": {
                    "slot": label,
                    "used": fit.getSlotsUsed(slot),
                    "total": fit.getNumSlots(slot),
                    "deficit": abs(free)
                }
            })

    turret_free = fit.getHardpointsFree(FittingHardpoint.TURRET)
    if turret_free < 0:
        issues.append({
            "code": "turret_hardpoints_exceeded",
            "message": "Turret hardpoints exceeded",
            "details": {
                "used": fit.getHardpointsUsed(FittingHardpoint.TURRET),
                "free": turret_free
            }
        })

    launcher_free = fit.getHardpointsFree(FittingHardpoint.MISSILE)
    if launcher_free < 0:
        issues.append({
            "code": "launcher_hardpoints_exceeded",
            "message": "Launcher hardpoints exceeded",
            "details": {
                "used": fit.getHardpointsUsed(FittingHardpoint.MISSILE),
                "free": launcher_free
            }
        })

    module_issues = []
    for idx, mod in enumerate(fit.modules):
        if mod.isEmpty:
            continue
        if not mod.isValidState(mod.state):
            module_issues.append({
                "slot_index": idx,
                "module_name": mod.item.typeName,
                "issue": "invalid_state",
                "state": int(mod.state)
            })
        can_have_state = mod.canHaveState(mod.state)
        if can_have_state is not True:
            module_issues.append({
                "slot_index": idx,
                "module_name": mod.item.typeName,
                "issue": "state_restricted",
                "state": int(mod.state),
                "max_allowed_state": int(can_have_state)
            })
        if mod.charge is not None and not mod.isValidCharge(mod.charge):
            module_issues.append({
                "slot_index": idx,
                "module_name": mod.item.typeName,
                "issue": "invalid_charge",
                "charge_name": mod.charge.typeName
            })

    if module_issues:
        issues.append({
            "code": "module_constraints",
            "message": "One or more modules violate fit/state/charge constraints",
            "details": {
                "count": len(module_issues),
                "modules": module_issues
            }
        })

    return {
        "fit_valid": bool(snapshot.get("fit_valid")),
        "passes_quality_gate": bool(snapshot.get("fit_valid")) and len(issues) == 0,
        "issue_count": len(issues),
        "issues": issues,
        "snapshot": snapshot,
        "profiles": {
            "damage_pattern": None if fit.damagePattern is None else fit.damagePattern.name,
            "target_profile": None if fit.targetProfile is None else fit.targetProfile.name
        }
    }


def _price_status_name(value):
    try:
        return SavedPriceStatus(value).name
    except Exception:
        return str(value)


def _fit_type_ids(fit):
    ids = set()
    if fit.ship is not None and fit.ship.item is not None:
        ids.add(fit.ship.item.ID)
    for mod in fit.modules:
        if mod.isEmpty:
            continue
        ids.add(mod.item.ID)
        if mod.charge is not None:
            ids.add(mod.charge.ID)
    for drone in fit.drones:
        if drone.item is not None:
            ids.add(drone.item.ID)
    for fighter in fit.fighters:
        if fighter.item is not None:
            ids.add(fighter.item.ID)
    for cargo in fit.cargo:
        if cargo.item is not None:
            ids.add(cargo.item.ID)
    return sorted(ids)


def _market_prices_for_type_ids(type_ids, include_unpriced=True):
    now = time()
    entries = []
    for type_id in type_ids:
        item = eos.db.getItem(type_id)
        if item is None:
            continue
        price_obj = eos.db.getPrice(type_id)
        if price_obj is None:
            if include_unpriced:
                entries.append({
                    "type_id": type_id,
                    "name": item.typeName,
                    "price": None,
                    "status": "missing",
                    "timestamp": None,
                    "age_seconds": None,
                    "is_valid": False
                })
            continue

        age = max(0.0, now - float(price_obj.time or 0))
        entries.append({
            "type_id": type_id,
            "name": item.typeName,
            "price": float(price_obj.price or 0),
            "status": _price_status_name(price_obj.status),
            "timestamp": float(price_obj.time or 0),
            "age_seconds": age,
            "is_valid": bool(price_obj.isValid())
        })

    entries.sort(key=lambda e: (e["price"] is None, e["name"]))
    return entries


def _iterative_optimize_fit(
    base_fit,
    objective="dps_at_range",
    direction="max",
    range_km=None,
    target_speed=0,
    target_sig=40,
    target_resists=None,
    max_passes=4,
    candidate_limit=12,
    query=None,
    require_fit_valid=True
):
    if direction not in ("max", "min"):
        raise ValueError("direction must be 'max' or 'min'")

    working_fit = deepcopy(base_fit)
    history = []
    passes = 0

    def evaluate(fit_obj):
        return _fit_snapshot(
            fit=fit_obj,
            range_km=range_km,
            target_speed=target_speed,
            target_sig=target_sig,
            target_resists=target_resists)

    current_snapshot = evaluate(working_fit)
    current_score = _objective_value(current_snapshot, objective)

    while passes < max_passes:
        passes += 1
        improved_this_pass = False

        for slot_index, mod in enumerate(working_fit.modules):
            if mod.isEmpty:
                continue

            candidates = _search_slot_candidates(
                fit=working_fit,
                slot_index=slot_index,
                query=query,
                limit=candidate_limit,
                include_variations=True)

            best_local = None
            for item in candidates:
                if item.typeName == mod.item.typeName:
                    continue
                trial_fit = deepcopy(working_fit)
                try:
                    _replace_module_in_fit(trial_fit, slot_index=slot_index, module_ref=item.ID)
                    trial_snapshot = evaluate(trial_fit)
                except Exception:
                    continue

                if require_fit_valid and not trial_snapshot.get("fit_valid"):
                    continue
                trial_score = _objective_value(trial_snapshot, objective)

                if best_local is None:
                    best_local = (trial_score, item, trial_snapshot)
                    continue

                if direction == "max" and trial_score > best_local[0]:
                    best_local = (trial_score, item, trial_snapshot)
                if direction == "min" and trial_score < best_local[0]:
                    best_local = (trial_score, item, trial_snapshot)

            if best_local is None:
                continue

            improved = best_local[0] > current_score if direction == "max" else best_local[0] < current_score
            if not improved:
                continue

            _replace_module_in_fit(working_fit, slot_index=slot_index, module_ref=best_local[1].ID)
            current_snapshot = best_local[2]
            current_score = best_local[0]
            improved_this_pass = True
            history.append({
                "pass": passes,
                "slot_index": slot_index,
                "slot": _slot_label(working_fit.modules[slot_index].slot),
                "module_name": best_local[1].typeName,
                "objective": objective,
                "score": current_score
            })

        if not improved_this_pass:
            break

    return {
        "fit": working_fit,
        "snapshot": current_snapshot,
        "objective": objective,
        "direction": direction,
        "score": current_score,
        "passes": passes,
        "changes": history
    }


def _optimize_fit_pareto(
    base_fit,
    range_km=None,
    target_speed=0,
    target_sig=40,
    target_resists=None,
    beam_width=16,
    require_fit_valid=True
):
    base_name = base_fit.name

    def evaluate(fit_obj):
        fit_obj.name = base_name
        snapshot = _fit_snapshot(
            fit=fit_obj,
            range_km=range_km,
            target_speed=target_speed,
            target_sig=target_sig,
            target_resists=target_resists)
        return {
            "fit": fit_obj,
            "snapshot": snapshot,
            "score": _score_from_snapshot(snapshot)
        }

    frontier = [evaluate(deepcopy(base_fit))]
    module_positions = [idx for idx, mod in enumerate(base_fit.modules) if not mod.isEmpty]

    for pos in module_positions:
        expanded = list(frontier)
        for candidate in frontier:
            fit_variant = candidate["fit"]
            mod = fit_variant.modules[pos]
            max_state = mod.getMaxState()
            states = [s for s in FittingModuleState if s <= max_state and mod.isValidState(s)]
            for state in states:
                if state == mod.state:
                    continue
                new_fit = deepcopy(fit_variant)
                new_fit.modules[pos].state = state
                expanded.append(evaluate(new_fit))
        if require_fit_valid:
            valid_expanded = [candidate for candidate in expanded if candidate["snapshot"].get("fit_valid")]
            if valid_expanded:
                expanded = valid_expanded
        frontier = _pareto_prune(expanded)
        frontier.sort(key=lambda c: (
            c["score"]["dps_at_range"],
            c["score"]["ehp"],
            c["score"]["sustainable_tank_total"],
            c["score"]["max_speed"],
            -c["score"]["align_time"]
        ), reverse=True)
        frontier = frontier[:beam_width]

    frontier = _pareto_prune(frontier)
    if require_fit_valid:
        valid_frontier = [candidate for candidate in frontier if candidate["snapshot"].get("fit_valid")]
        if valid_frontier:
            return valid_frontier
    return frontier


class FitSessionStore:

    def __init__(self):
        self._fits = {}

    def put(self, fit):
        fit_id = str(uuid.uuid4())
        self._fits[fit_id] = fit
        return fit_id

    def get(self, fit_id):
        return self._fits.get(fit_id)

    def update(self, fit_id, fit):
        self._fits[fit_id] = fit


class MCPServer:

    def __init__(self):
        self.sessions = FitSessionStore()
        self._last_fit_id = None

    def _resolve_fit(self, arguments):
        fit_id = arguments.get("fit_id") or self._last_fit_id
        if fit_id is None:
            raise ValueError("fit_id is required (or import a fit first)")
        fit = self.sessions.get(fit_id)
        if fit is None:
            raise ValueError("unknown fit_id")
        return fit_id, fit

    def _tools(self):
        return [
            {
                "name": "fit_import_text",
                "description": "Import EVE fit text (EFT/pyfa copy-paste style) into an ephemeral headless session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "name": {"type": "string"},
                        "character_profile": {"type": "string", "enum": ["all5", "all0"]}
                    },
                    "required": ["text"]
                }
            },
            {
                "name": "hull_get_slot_summary",
                "description": "Return authoritative slot/hardpoint summary for a hull using pyfa's own fitting engine data.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "hull": {"type": ["string", "integer"]},
                        "character_profile": {"type": "string", "enum": ["all5", "all0"]}
                    },
                    "required": ["hull"]
                }
            },
            {
                "name": "fit_get_stats",
                "description": "Get full ship stats for a session fit, including applied DPS at range. Agent guidance: call this as a verification step before returning user-facing fitting conclusions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fit_id": {"type": "string"},
                        "range_km": {"type": ["number", "null"]},
                        "target_speed": {"type": "number"},
                        "target_sig": {"type": "number"},
                        "target_resists": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4
                        }
                    },
                    "required": ["fit_id"]
                }
            },
            {
                "name": "market_get_prices",
                "description": "Return cached pyfa market price data (price/status/age/validity) for explicit items or all items used by a fit.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fit_id": {"type": "string"},
                        "items": {
                            "type": "array",
                            "items": {"type": ["string", "integer"]},
                            "minItems": 1,
                            "maxItems": 512
                        },
                        "include_unpriced": {"type": "boolean"}
                    }
                }
            },
            {
                "name": "fit_set_profiles",
                "description": "Set target profile and/or damage pattern on a fit using pyfa builtins or user-defined profiles, then return updated stats.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fit_id": {"type": "string"},
                        "target_profile": {"type": ["string", "integer", "null"]},
                        "damage_pattern": {"type": ["string", "integer", "null"]},
                        "range_km": {"type": ["number", "null"]},
                        "target_speed": {"type": "number"},
                        "target_sig": {"type": "number"},
                        "target_resists": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4
                        }
                    },
                    "required": ["fit_id"]
                }
            },
            {
                "name": "fit_validate_and_explain",
                "description": "Run pyfa fit validity checks and return actionable reasons if invalid before agent recommendations.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fit_id": {"type": "string"}
                    },
                    "required": ["fit_id"]
                }
            },
            {
                "name": "fit_optimize_pareto",
                "description": "Tune module states on the submitted fit and return Pareto-optimal variants across damage, tank, cap, and mobility stats. Agent guidance: use this to validate/improve candidate fits before user-facing recommendations.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fit_id": {"type": "string"},
                        "range_km": {"type": ["number", "null"]},
                        "target_speed": {"type": "number"},
                        "target_sig": {"type": "number"},
                        "target_resists": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4
                        },
                        "beam_width": {"type": "integer", "minimum": 4, "maximum": 64},
                        "require_fit_valid": {"type": "boolean"}
                    },
                    "required": ["fit_id"]
                }
            },
            {
                "name": "fit_list_modules",
                "description": "List all module slots in the fit, including slot indexes for agent-driven edits.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fit_id": {"type": "string"}
                    },
                    "required": ["fit_id"]
                }
            },
            {
                "name": "fit_compare_slot_candidates",
                "description": "Compare candidate modules for one slot by simulating swap-in and returning stats/objective deltas.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fit_id": {"type": "string"},
                        "slot_index": {"type": "integer", "minimum": 0},
                        "query": {"type": "string"},
                        "candidate_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 64
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 64},
                        "objective": {
                            "type": "string",
                            "enum": ["dps", "dps_at_range", "ehp", "tank", "sustainable_tank", "speed", "align_time"]
                        },
                        "direction": {"type": "string", "enum": ["max", "min"]},
                        "range_km": {"type": ["number", "null"]},
                        "target_speed": {"type": "number"},
                        "target_sig": {"type": "number"},
                        "target_resists": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4
                        },
                        "require_fit_valid": {"type": "boolean"}
                    },
                    "required": ["fit_id", "slot_index"]
                }
            },
            {
                "name": "fit_apply_slot_candidate",
                "description": "Apply a module replacement to a specific slot index and return updated fit snapshot.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fit_id": {"type": "string"},
                        "slot_index": {"type": "integer", "minimum": 0},
                        "module_name": {"type": ["string", "null"]},
                        "charge_name": {"type": ["string", "null"]},
                        "state": {"type": ["integer", "null"], "minimum": -1, "maximum": 2},
                        "range_km": {"type": ["number", "null"]},
                        "target_speed": {"type": "number"},
                        "target_sig": {"type": "number"},
                        "target_resists": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4
                        }
                    },
                    "required": ["fit_id", "slot_index"]
                }
            },
            {
                "name": "fit_optimize_iterative",
                "description": "Iteratively compare and swap modules across slots until objective score converges. Agent guidance: run this (or fit_optimize_pareto) before presenting final fit advice.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fit_id": {"type": "string"},
                        "objective": {
                            "type": "string",
                            "enum": ["dps", "dps_at_range", "ehp", "tank", "sustainable_tank", "speed", "align_time"]
                        },
                        "direction": {"type": "string", "enum": ["max", "min"]},
                        "range_km": {"type": ["number", "null"]},
                        "target_speed": {"type": "number"},
                        "target_sig": {"type": "number"},
                        "target_resists": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4
                        },
                        "query": {"type": "string"},
                        "max_passes": {"type": "integer", "minimum": 1, "maximum": 20},
                        "candidate_limit": {"type": "integer", "minimum": 2, "maximum": 64},
                        "require_fit_valid": {"type": "boolean"}
                    },
                    "required": ["fit_id"]
                }
            },
            {
                "name": "fit_export_eft",
                "description": "Export an in-session fit back to EFT text.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fit_id": {"type": "string"}
                    },
                    "required": ["fit_id"]
                }
            }
        ]

    def _call_tool(self, name, arguments):
        _ensure_engine()

        # Keep dotted aliases for backward compatibility with older scripts/clients
        if name in ("fit_import_text", "fit.import_text"):
            text = arguments.get("text") or arguments.get("fit_text")
            if not text:
                raise ValueError("text is required")
            fit = _import_eft_text(text, character_profile=arguments.get("character_profile", "all5"))
            if arguments.get("name"):
                fit.name = arguments["name"]
            fit_id = self.sessions.put(fit)
            self._last_fit_id = fit_id
            snapshot = _fit_snapshot(fit)
            return {
                "fit_id": fit_id,
                "snapshot": snapshot
            }

        if name in ("hull_get_slot_summary", "hull.get_slot_summary"):
            hull_ref = arguments.get("hull")
            if hull_ref is None:
                raise ValueError("hull is required")
            return _hull_slot_summary(
                hull_ref=hull_ref,
                character_profile=arguments.get("character_profile", "all5"))

        if name in ("fit_get_stats", "fit.get_stats"):
            fit_id, fit = self._resolve_fit(arguments)
            self._last_fit_id = fit_id
            snapshot = _fit_snapshot(
                fit,
                range_km=arguments.get("range_km"),
                target_speed=arguments.get("target_speed", 0),
                target_sig=arguments.get("target_sig", 40),
                target_resists=tuple(arguments.get("target_resists", (0, 0, 0, 0))))
            return snapshot

        if name in ("market_get_prices", "market.get_prices"):
            include_unpriced = arguments.get("include_unpriced", True)
            requested_items = arguments.get("items") or []
            fit_id = arguments.get("fit_id")

            type_ids = set()
            if fit_id is not None:
                fit = self.sessions.get(fit_id)
                if fit is None:
                    raise ValueError("unknown fit_id")
                type_ids.update(_fit_type_ids(fit))

            for ref in requested_items:
                item = eos.db.getItem(ref)
                if item is not None:
                    type_ids.add(item.ID)

            if not type_ids:
                raise ValueError("provide fit_id and/or items")

            entries = _market_prices_for_type_ids(sorted(type_ids), include_unpriced=include_unpriced)
            total_price = sum(e["price"] for e in entries if e["price"] is not None)
            priced_count = sum(1 for e in entries if e["price"] is not None)
            return {
                "fit_id": fit_id,
                "count": len(entries),
                "priced_count": priced_count,
                "total_price": total_price,
                "entries": entries
            }

        if name in ("fit_set_profiles", "fit.set_profiles"):
            fit_id, fit = self._resolve_fit(arguments)
            self._last_fit_id = fit_id

            if "damage_pattern" in arguments:
                fit.damagePattern = _resolve_damage_pattern_ref(arguments.get("damage_pattern"))
            if "target_profile" in arguments:
                fit.targetProfile = _resolve_target_profile_ref(arguments.get("target_profile"))

            self.sessions.update(fit_id, fit)
            snapshot = _fit_snapshot(
                fit,
                range_km=arguments.get("range_km"),
                target_speed=arguments.get("target_speed", 0),
                target_sig=arguments.get("target_sig", 40),
                target_resists=tuple(arguments.get("target_resists", (0, 0, 0, 0))))
            return {
                "fit_id": fit_id,
                "profiles": {
                    "damage_pattern": None if fit.damagePattern is None else fit.damagePattern.name,
                    "target_profile": None if fit.targetProfile is None else fit.targetProfile.name
                },
                "snapshot": snapshot
            }

        if name in ("fit_validate_and_explain", "fit.validate_and_explain"):
            fit_id, fit = self._resolve_fit(arguments)
            self._last_fit_id = fit_id
            result = _validate_fit_explain(fit)
            result["fit_id"] = fit_id
            return result

        if name in ("fit_optimize_pareto", "fit.optimize_pareto"):
            fit_id, fit = self._resolve_fit(arguments)
            self._last_fit_id = fit_id
            frontier = _optimize_fit_pareto(
                base_fit=fit,
                range_km=arguments.get("range_km"),
                target_speed=arguments.get("target_speed", 0),
                target_sig=arguments.get("target_sig", 40),
                target_resists=tuple(arguments.get("target_resists", (0, 0, 0, 0))),
                beam_width=arguments.get("beam_width", 16),
                require_fit_valid=arguments.get("require_fit_valid", True))
            variants = []
            for node in frontier:
                variant_fit = node["fit"]
                variant_id = self.sessions.put(variant_fit)
                variants.append({
                    "fit_id": variant_id,
                    "snapshot": node["snapshot"]
                })
            return {
                "count": len(variants),
                "variants": variants
            }

        if name in ("fit_list_modules", "fit.list_modules"):
            fit_id, fit = self._resolve_fit(arguments)
            self._last_fit_id = fit_id
            return {
                "fit_id": fit_id,
                "modules": _fit_module_listing(fit)
            }

        if name in ("fit_compare_slot_candidates", "fit.compare_slot_candidates"):
            fit_id, fit = self._resolve_fit(arguments)
            self._last_fit_id = fit_id

            slot_index = int(arguments.get("slot_index"))
            objective = arguments.get("objective", "dps_at_range")
            direction = arguments.get("direction", "max")
            require_fit_valid = arguments.get("require_fit_valid", True)

            base_snapshot = _fit_snapshot(
                fit=fit,
                range_km=arguments.get("range_km"),
                target_speed=arguments.get("target_speed", 0),
                target_sig=arguments.get("target_sig", 40),
                target_resists=tuple(arguments.get("target_resists", (0, 0, 0, 0))))
            base_score = _objective_value(base_snapshot, objective)

            explicit_candidates = arguments.get("candidate_names")
            if explicit_candidates:
                candidate_items = []
                for name_or_id in explicit_candidates:
                    item = eos.db.getItem(name_or_id, eager=("attributes", "group.category"))
                    if item is not None:
                        candidate_items.append(item)
            else:
                candidate_items = _search_slot_candidates(
                    fit=fit,
                    slot_index=slot_index,
                    query=arguments.get("query"),
                    limit=int(arguments.get("limit", 24)),
                    include_variations=True)

            comparisons = []
            for item in candidate_items:
                trial_fit = deepcopy(fit)
                try:
                    _replace_module_in_fit(trial_fit, slot_index=slot_index, module_ref=item.ID)
                    trial_snapshot = _fit_snapshot(
                        fit=trial_fit,
                        range_km=arguments.get("range_km"),
                        target_speed=arguments.get("target_speed", 0),
                        target_sig=arguments.get("target_sig", 40),
                        target_resists=tuple(arguments.get("target_resists", (0, 0, 0, 0))))
                except Exception:
                    continue

                if require_fit_valid and not trial_snapshot.get("fit_valid"):
                    continue

                trial_score = _objective_value(trial_snapshot, objective)
                delta = trial_score - base_score
                comparisons.append({
                    "module_name": item.typeName,
                    "snapshot": trial_snapshot,
                    "objective": objective,
                    "score": trial_score,
                    "delta": delta,
                    "improves": delta > 0 if direction == "max" else delta < 0
                })

            comparisons.sort(key=lambda row: row["score"], reverse=(direction == "max"))
            return {
                "fit_id": fit_id,
                "slot_index": slot_index,
                "objective": objective,
                "direction": direction,
                "base_score": base_score,
                "comparisons": comparisons
            }

        if name in ("fit_apply_slot_candidate", "fit.apply_slot_candidate"):
            fit_id, fit = self._resolve_fit(arguments)
            self._last_fit_id = fit_id

            _replace_module_in_fit(
                fit,
                slot_index=int(arguments.get("slot_index")),
                module_ref=arguments.get("module_name"),
                charge_ref=arguments.get("charge_name"),
                state=arguments.get("state"))

            self.sessions.update(fit_id, fit)
            snapshot = _fit_snapshot(
                fit,
                range_km=arguments.get("range_km"),
                target_speed=arguments.get("target_speed", 0),
                target_sig=arguments.get("target_sig", 40),
                target_resists=tuple(arguments.get("target_resists", (0, 0, 0, 0))))
            return {
                "fit_id": fit_id,
                "snapshot": snapshot,
                "modules": _fit_module_listing(fit)
            }

        if name in ("fit_optimize_iterative", "fit.optimize_iterative"):
            fit_id, fit = self._resolve_fit(arguments)
            self._last_fit_id = fit_id

            result = _iterative_optimize_fit(
                base_fit=fit,
                objective=arguments.get("objective", "dps_at_range"),
                direction=arguments.get("direction", "max"),
                range_km=arguments.get("range_km"),
                target_speed=arguments.get("target_speed", 0),
                target_sig=arguments.get("target_sig", 40),
                target_resists=tuple(arguments.get("target_resists", (0, 0, 0, 0))),
                max_passes=int(arguments.get("max_passes", 4)),
                candidate_limit=int(arguments.get("candidate_limit", 12)),
                query=arguments.get("query"),
                require_fit_valid=arguments.get("require_fit_valid", True))

            optimized_fit = result["fit"]
            optimized_fit_id = self.sessions.put(optimized_fit)
            return {
                "fit_id": optimized_fit_id,
                "objective": result["objective"],
                "direction": result["direction"],
                "score": result["score"],
                "passes": result["passes"],
                "changes": result["changes"],
                "snapshot": result["snapshot"]
            }

        if name in ("fit_export_eft", "fit.export_eft"):
            fit_id, fit = self._resolve_fit(arguments)
            self._last_fit_id = fit_id
            return {
                "eft": _export_eft_text(fit)
            }

        raise ValueError("unknown tool")

    def _handle_request(self, request):
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params", {})

        if method == "initialize":
            requested_protocol = params.get("protocolVersion", "2024-11-05")
            negotiated_protocol = requested_protocol if requested_protocol in SUPPORTED_PROTOCOLS else "2024-11-05"
            _log(f"initialize requested={requested_protocol} negotiated={negotiated_protocol}")
            return _jsonrpc_ok(req_id, {
                "protocolVersion": negotiated_protocol,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "pyfa-headless", "version": "0.1.0"}
            })

        if method == "tools/list":
            return _jsonrpc_ok(req_id, {"tools": self._tools()})

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            payload = self._call_tool(name, arguments)
            return _jsonrpc_ok(req_id, {
                "content": [{"type": "text", "text": json.dumps(_coerce_json(payload))}]
            })

        if method == "notifications/initialized" or req_id is None:
            return None

        return _jsonrpc_err(req_id, -32601, f"method not found: {method}")

    def run(self):
        _log(f"run loop start pid={os.getpid()}")
        while True:
            request = None
            try:
                request = _read_message(sys.stdin)
                if request is None:
                    _log("stdin closed or empty message; stopping run loop")
                    break
                response = self._handle_request(request)
            except Exception as ex:
                _log(f"request handling exception: {ex}\n{traceback.format_exc()}")
                req_id = request.get("id") if isinstance(request, dict) else None
                response = _jsonrpc_err(req_id, -32000, str(ex))
            if response is not None:
                try:
                    _write_message(sys.stdout, response)
                except Exception as ex:
                    _log(f"write exception: {ex}\n{traceback.format_exc()}")
                    break


def run_stdio_server():
    _log("boot")
    server = MCPServer()
    server.run()


if __name__ == "__main__":
    run_stdio_server()