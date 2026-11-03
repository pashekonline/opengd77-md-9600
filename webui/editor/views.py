from __future__ import annotations

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from cp_utils import (
    COLOR_CODE_MAX,
    MODES,
    NAME_MAX_LEN,
    _channel_valid,
    freq_to_str,
    tone_to_str,
)
from gd77_zones import (
    format_channel_zones,
    get_zones,
    validate_zone_name,
    zone_capacity,
    zone_id,
    zone_members_map,
    zone_name,
)

from . import logic, sats
from .radio import radio
from .state import ALL_FILTER, APP_TITLE, state


def _status_ctx() -> dict:
    return {
        "port": state.port,
        "total_channels": state.total_channels(),
        "zones_count": len(state.zone_labels()),
        "dirty": state.dirty,
        "loaded": state.data is not None,
        "radio_busy": radio.busy(),
        "can_undo": bool(state._undo),
        "can_redo": bool(state._redo),
    }


def page(request, template: str, ctx: dict) -> HttpResponse:
    ctx = dict(ctx)
    ctx.setdefault("status", _status_ctx())
    ctx.setdefault("app_title", APP_TITLE)
    msg = state.take_message()

    if msg:
        ctx["message"], ctx["message_error"] = msg

    return render(request, template, ctx)


# ---------------------------------------------------------------------------
# Каналы
# ---------------------------------------------------------------------------

def index(request):
    q = (request.GET.get("q") or "").strip()
    zone_filter = request.GET.get("zone") or ALL_FILTER
    mode_filter = request.GET.get("mode") or ALL_FILTER

    cp = state.codeplug()
    rows: list[dict] = []
    total = 0

    if cp is not None:
        needle = q.casefold()

        for ch in cp.channels:
            if not _channel_valid(ch):
                continue

            total += 1

            if zone_filter != ALL_FILTER:
                try:
                    zid = int(zone_filter)
                except ValueError:
                    zid = None

                if zid is not None and not any(
                    z == zid for z, _ in state.zone_map.get(ch.index, [])
                ):
                    continue

            if (
                mode_filter != ALL_FILTER
                and MODES.get(ch.mode, "?") != mode_filter
            ):
                continue

            if needle:
                haystack = " ".join([
                    ch.name,
                    freq_to_str(ch.rx_freq),
                    freq_to_str(ch.tx_freq),
                    MODES.get(ch.mode, ""),
                    format_channel_zones(state.zone_map, ch.index),
                    str(ch.tx_color),
                    str(ch.contact_num),
                ]).casefold()

                if needle not in haystack:
                    continue

            rows.append({
                "index": ch.index,
                "num": ch.index + 1,
                "name": ch.name,
                "rx": freq_to_str(ch.rx_freq),
                "tx": freq_to_str(ch.tx_freq),
                "mode": MODES.get(ch.mode, "?"),
                "rx_tone": tone_to_str(ch, "rx_tone") or "—",
                "tx_tone": tone_to_str(ch, "tx_tone") or "—",
                "cc": str(ch.tx_color) if ch.mode == 1 else "—",
                "contact": str(ch.contact_num) if ch.mode == 1 else "—",
                "zones": format_channel_zones(state.zone_map, ch.index) or "—",
            })

    return page(request, "editor/index.html", {
        "rows": rows,
        "total": total,
        "q": q,
        "zone_filter": zone_filter,
        "mode_filter": mode_filter,
        "zone_options": state.zone_labels(),
        "filtered": bool(q or zone_filter != ALL_FILTER or mode_filter != ALL_FILTER),
    })


def _form_values(channel) -> dict:
    if channel is None:
        return {
            "name": "",
            "rx": "",
            "tx": "",
            "mode": "FM",
            "rx_tone": "",
            "tx_tone": "",
            "cc": "0",
            "contact": "0",
        }

    return {
        "name": channel.name,
        "rx": freq_to_str(channel.rx_freq),
        "tx": freq_to_str(channel.tx_freq),
        "mode": MODES.get(channel.mode, "FM"),
        "rx_tone": tone_to_str(channel, "rx_tone"),
        "tx_tone": tone_to_str(channel, "tx_tone"),
        "cc": str(channel.tx_color or 0),
        "contact": str(channel.contact_num or 0),
    }


def channel_edit(request, index: int | None = None):
    if state.data is None:
        state.notify("Сначала прочитайте кодплаг")
        return redirect("index")

    channel = None

    if index is not None:
        try:
            channel = state.codeplug().channels[index]
        except Exception:
            state.notify(f"Канал #{index + 1} не найден", error=True)
            return redirect("index")

    zone_labels = state.zone_labels()
    checked = (
        {zid for zid, _ in state.zone_map.get(index, [])}
        if index is not None
        else set()
    )

    if request.method == "POST":
        data = request.POST
        values, errors = logic.validate_channel_form(data)

        zones: set[int] = set()

        for token in data.getlist("zones"):
            try:
                zones.add(int(token))
            except ValueError:
                pass

        if not errors:
            ok, message = logic.apply_channel_edit(
                state, index, values, zones
            )

            if ok:
                return redirect("index")

            errors["form"] = message

        vals = {k: data.get(k, "") for k in (
            "name", "rx", "tx", "mode", "rx_tone", "tx_tone", "cc", "contact"
        )}
        zones = {int(t) for t in data.getlist("zones") if t.isdigit()}

        return page(request, "editor/channel.html", {
            "values": vals,
            "errors": errors,
            "zone_labels": zone_labels,
            "checked_zones": zones,
            "edit_index": index,
            "title": "Новый канал" if index is None else f"Канал #{index + 1}",
            "color_codes": range(COLOR_CODE_MAX + 1),
            "name_max": NAME_MAX_LEN,
        })

    return page(request, "editor/channel.html", {
        "values": _form_values(channel),
        "errors": {},
        "zone_labels": zone_labels,
        "checked_zones": checked,
        "edit_index": index,
        "title": "Новый канал" if index is None else f"Канал #{index + 1}",
        "color_codes": range(COLOR_CODE_MAX + 1),
        "name_max": NAME_MAX_LEN,
    })


def channel_delete(request, index: int):
    if request.method == "POST":
        ok, message = logic.delete_channel(state, index)

        if not ok:
            state.notify(message, error=True)

        return redirect("index")

    name = f"#{index + 1}"

    try:
        ch = state.codeplug().channels[index]
        name = ch.name or name
    except Exception:
        pass

    return page(request, "editor/confirm.html", {
        "title": "Удаление канала",
        "message": f"Удалить канал #{index + 1} «{name}»?\n\n"
                   "Канал будет очищен и убран из всех зон.",
        "confirm_label": "Удалить",
        "action": f"/channel/{index}/delete/",
        "danger": True,
    })


@require_POST
def channel_duplicate(request, index: int):
    ok, message = logic.duplicate_channel(state, index)
    state.notify(message, error=not ok)
    return redirect("index")


@require_POST
def undo(request):
    message = state.undo()
    state.notify(message or "Нечего отменять", error=message is None)
    return redirect("index")


@require_POST
def redo(request):
    message = state.redo()
    state.notify(message or "Нечего возвращать", error=message is None)
    return redirect("index")


# ---------------------------------------------------------------------------
# Зоны
# ---------------------------------------------------------------------------

def zones(request):
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create":
            name = request.POST.get("name", "").strip()
            cp = state.codeplug()

            try:
                validate_zone_name(name, cp)
            except Exception as e:
                state.notify(str(e), error=True)
                return redirect("zones")

            ok, message = logic.create_zone(state, name)
            state.notify(message, error=not ok)

        elif action == "rename":
            zid = int(request.POST.get("zid", "-1"))
            name = request.POST.get(f"name_{zid}", "").strip()
            cp = state.codeplug()

            try:
                validate_zone_name(name, cp, exclude_zid=zid)
            except Exception as e:
                state.notify(str(e), error=True)
                return redirect("zones")

            ok, message = logic.rename_zone(state, zid, name)
            state.notify(message, error=not ok)

        elif action == "delete":
            zid = int(request.POST.get("zid", "-1"))
            ok, message = logic.delete_zone(state, zid)
            state.notify(message, error=not ok)

        return redirect("zones")

    cp = state.codeplug()
    items = []

    if cp is not None:
        members = zone_members_map(cp)

        for i, z in enumerate(get_zones(cp)):
            zid = zone_id(z, i)
            items.append({
                "zid": zid,
                "name": zone_name(z, zid),
                "members": len(members.get(zid, [])),
                "capacity": zone_capacity(z),
            })

    return page(request, "editor/zones.html", {"zones": items})


def zone_detail(request, zid: int):
    cp = state.codeplug()
    rows = []

    if cp is not None:
        for idx in zone_members_map(cp).get(zid, []):
            try:
                ch = cp.channels[idx]
                rows.append({
                    "num": idx + 1,
                    "name": ch.name or "(пусто)",
                    "rx": freq_to_str(ch.rx_freq) if ch.rx_freq else "—",
                    "tx": freq_to_str(ch.tx_freq) if ch.tx_freq else "—",
                })
            except Exception:
                rows.append(
                    {"num": idx + 1, "name": "(ошибка)", "rx": "—", "tx": "—"}
                )

    return page(request, "editor/zone_detail.html", {"zid": zid, "rows": rows})


# ---------------------------------------------------------------------------
# Рация
# ---------------------------------------------------------------------------

@require_POST
def radio_read(request):
    if radio.busy():
        state.notify("Операция с рацией уже выполняется", error=True)
        return redirect("index")

    if state.dirty and state.data is not None:
        return redirect("radio_read_confirm")

    err = radio.submit("read")

    if err:
        state.notify(err, error=True)

    return redirect("index")


def radio_read_confirm(request):
    if request.method == "POST":
        err = radio.submit("read")

        if err:
            state.notify(err, error=True)

        return redirect("index")

    return page(request, "editor/confirm.html", {
        "title": "Чтение из рации",
        "message": "Есть несохранённые изменения.\n\n"
                   "При чтении они будут потеряны. Продолжить?",
        "confirm_label": "Прочитать",
        "action": "/radio/read/confirm/",
        "danger": True,
    })


def radio_write(request):
    if state.data is None:
        state.notify("Сначала прочитайте кодплаг", error=True)
        return redirect("index")

    if request.method == "POST":
        if radio.busy():
            state.notify("Операция с рацией уже выполняется", error=True)
            return redirect("index")

        if request.POST.get("confirm_text", "").strip() != "WRITE":
            return page(request, "editor/write.html", {
                "stats": _write_stats(),
                "error": "Введите WRITE заглавными буквами",
            })

        with state.lock:
            snapshot = bytes(state.data)

        err = radio.submit("write", snapshot=snapshot)

        if err:
            state.notify(err, error=True)

        return redirect("index")

    if radio.busy():
        state.notify("Операция с рацией уже выполняется", error=True)
        return redirect("index")

    return page(request, "editor/write.html", {
        "stats": _write_stats(),
        "error": None,
    })


def _write_stats() -> dict:
    backup = "codeplug_before_write_<время>.g77"

    return {
        "total": state.total_channels(),
        "zones": len(state.zone_labels()),
        "changed": state.changed_channel_count(),
        "backup": backup,
    }


@require_POST
def radio_verify(request):
    if state.data is None:
        state.notify("Сначала прочитайте кодплаг", error=True)
        return redirect("index")

    err = radio.submit("verify")

    if err:
        state.notify(err, error=True)

    return redirect("index")


def api_job(request):
    return JsonResponse(radio.api_status())


@require_POST
def api_job_cancel(request):
    return JsonResponse({"ok": radio.request_cancel()})


# ---------------------------------------------------------------------------
# Спутники
# ---------------------------------------------------------------------------

def sats_page(request):
    if request.method == "POST":
        action = request.POST.get("action")

        if action in ("fetch_tle", "write"):
            kind = {"fetch_tle": "tle_fetch", "write": "sat_write"}[action]
            err = radio.submit(kind)

            if err:
                state.notify(err, error=True)

        elif action == "delete":
            slot = int(request.POST.get("slot", "-1"))

            try:
                cfg = sats.read_config()

                if 0 <= slot < len(cfg):
                    name = cfg[slot].get("name", str(slot))
                    cfg.pop(slot)
                    sats.save_config(cfg)
                    state.notify(f"Спутник «{name}» удалён из списка")
            except Exception as e:
                state.notify(f"Не удалось удалить: {e}", error=True)

        return redirect("satellites")

    try:
        cfg = sats.read_config()
        config_error = None
    except Exception as e:
        cfg = []
        config_error = str(e)

    rows = []

    def g(sat, key):
        v = sat.get(key)
        return "—" if v in (None, "") else v

    for i, s in enumerate(cfg):
        tle = sats.tle_for(s, state.sat_tle)

        rows.append({
            "slot": i,
            "name": s.get("name", ""),
            "norad": s.get("norad_id") or "—",
            "rx1": g(s, "rx_freq_mhz"),
            "tx1": g(s, "tx_freq_mhz"),
            "ctcss": g(s, "ctcss_tx_hz"),
            "arm": g(s, "arm_ctcss_hz"),
            "rx2": g(s, "secondary_rx_freq_mhz"),
            "tx2": g(s, "secondary_tx_freq_mhz"),
            "rx3": g(s, "tertiary_rx_freq_mhz"),
            "tx3": g(s, "tertiary_tx_freq_mhz"),
            "epoch": sats.epoch_of(tle[0]) if tle else "—",
            "tle1": tle[0] if tle else "",
            "tle2": tle[1] if tle else "",
        })

    return page(request, "editor/sats.html", {
        "rows": rows,
        "config_error": config_error,
        "busy": radio.busy(),
    })


_SAT_FIELDS = (
    "name", "norad_id", "rx_freq_mhz", "tx_freq_mhz", "ctcss_tx_hz",
    "arm_ctcss_hz", "secondary_rx_freq_mhz", "secondary_tx_freq_mhz",
    "tertiary_rx_freq_mhz", "tertiary_tx_freq_mhz",
)


def satellite_edit(request, slot: int | None = None):
    try:
        cfg = sats.read_config()
    except Exception as e:
        state.notify(f"Не удалось прочитать конфиг: {e}", error=True)
        return redirect("satellites")

    current = {}

    if slot is not None:
        if not 0 <= slot < len(cfg):
            state.notify("Спутник не найден", error=True)
            return redirect("satellites")

        current = cfg[slot]

    if request.method == "POST":
        data = {k: request.POST.get(k, "") for k in _SAT_FIELDS}
        row, errors = sats.validate_sat_form(data, cfg, slot)

        if not errors:
            keep = {k: v for k, v in current.items()
                    if k not in sats.JSON_KEYS}
            full = {**keep, **row}

            if slot is None:
                cfg.append(full)
            else:
                cfg[slot] = full

            try:
                sats.save_config(cfg)
            except Exception as e:
                state.notify(f"Не удалось сохранить JSON: {e}", error=True)
                return redirect("satellites")

            # имя могло измениться — сбрасываем кэш TLE по старому имени
            old_name = current.get("name")

            if old_name and old_name != row["name"]:
                state.sat_tle.pop(old_name, None)

            state.notify(
                f"Спутник «{row['name']}» "
                + ("добавлен" if slot is None else "сохранён")
                + ". Не забудьте «Найти TLE» и «Записать в рацию»."
            )
            return redirect("satellites")

        values = data
    else:
        values = {k: ("" if current.get(k) is None else current.get(k))
                  for k in _SAT_FIELDS}
        errors = {}

    return page(request, "editor/satellite_form.html", {
        "values": values,
        "errors": errors,
        "slot": slot,
        "title": (
            f"Спутник {current.get('name', '')}"
            if slot is not None
            else "Новый спутник"
        ),
        "name_max": sats.MAX_NAME_LEN,
    })


def norad_search(request):
    q = (request.GET.get("q") or "").strip()

    if len(q) < 2:
        return JsonResponse({"results": [], "error": None})

    try:
        results = sats.find_norad(q)
        error = None
    except Exception as e:
        results, error = [], str(e)

    return JsonResponse({"results": results, "error": error})


def transmitters_search(request):
    nid = (request.GET.get("norad") or "").strip()

    if not nid.isdigit():
        return JsonResponse({"rows": [], "error": None})

    try:
        rows = sats.find_transmitters(int(nid))
        error = None
    except Exception as e:
        rows, error = [], str(e)

    return JsonResponse({"rows": rows, "error": error, "norad": int(nid)})


# ---------------------------------------------------------------------------
# CSV / backup / журнал
# ---------------------------------------------------------------------------

def csv_export(request):
    if state.data is None:
        state.notify("Сначала прочитайте кодплаг", error=True)
        return redirect("index")

    try:
        name, content = logic.export_csv(state)
    except Exception as e:
        state.notify(f"Ошибка экспорта: {e}", error=True)
        return redirect("index")

    state.notify(f"Экспортировано: {name}")
    response = HttpResponse(
        content.encode("utf-8-sig"), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


def csv_import(request):
    if state.data is None:
        state.notify("Сначала прочитайте кодплаг", error=True)
        return redirect("index")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "apply":
            pending = state.pending_import

            if not pending:
                state.notify("Предпросмотр утерян, начните импорт заново", error=True)
                return redirect("csv_import")

            ok, message = logic.apply_import(state, pending["rows"])
            state.pending_import = None

            if not ok:
                state.notify(message, error=True)
                return redirect("csv_import")

            state.notify(message)
            return redirect("index")

        if action == "cancel":
            state.pending_import = None
            return redirect("index")

        upload = request.FILES.get("file")

        if upload is None:
            state.notify("Файл не выбран", error=True)
            return redirect("csv_import")

        try:
            text = upload.read().decode("utf-8-sig", errors="replace")
            rows, errors = logic.parse_csv(state, text)
        except Exception as e:
            state.notify(f"Ошибка CSV: {e}", error=True)
            return redirect("csv_import")

        if not rows and not errors:
            state.notify("CSV пуст: нет строк для импорта", error=True)
            return redirect("csv_import")

        state.pending_import = {
            "filename": upload.name,
            "rows": rows,
            "errors": errors,
        }

    pending = state.pending_import

    if pending:
        zone_warnings = sum(1 for r in pending["rows"] if r.zone_error)
        messages = [f"! {line}" for line in pending["errors"][:20]]

        for r in pending["rows"]:
            if r.zone_error:
                messages.append(f"! Строка {r.line}: зоны — {r.zone_error}")

        if len(pending["errors"]) > 20:
            messages.append(f"… и ещё ошибок: {len(pending['errors']) - 20}")

        return page(request, "editor/csv_preview.html", {
            "pending": pending,
            "preview_rows": [{
                "line": r.line,
                "name": r.name,
                "rx": freq_to_str(r.rx),
                "tx": freq_to_str(r.tx),
                "mode": MODES.get(r.mode, "?"),
                "zones": ",".join(str(z) for z in sorted(r.zones)),
            } for r in pending["rows"][:20]],
            "zone_warnings": zone_warnings,
            "messages": messages or ["Ошибок нет."],
        })

    return page(request, "editor/csv_import.html", {})


@require_POST
def backup(request):
    ok, message = logic.make_backup(state)

    if not ok:
        state.notify(message, error=True)

    return redirect("index")


def journal(request):
    return page(request, "editor/journal.html", {"events": state.events()})


def help_page(request):
    return page(request, "editor/help.html", {})
