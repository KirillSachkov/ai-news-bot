"""The live loop: poll sources and Telegram updates without blocking either.

Runs on a personal machine (launchd/cron/systemd) with long polling, so no
public webhook and no server are needed.

Two threads, because one could not do both jobs:

  * the main thread owns Telegram - long polling, commands, buttons, and every
    outgoing message, so sends are never interleaved;
  * a worker thread owns collection, on two cadences: a short one for the
    handful of sources worth watching minute by minute, and the long one for
    everything else.

Before this split a fetch blocked the update loop for one to four minutes, so
the bot went deaf exactly while it was busiest - and no amount of tuning the
intervals could produce "tell me the moment it happens".
"""

from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import pipeline, render
from .store import Store, utc_now

HELP = (
    "<b>Команды</b>\n"
    "/status — состояние, лимиты, молчащие источники\n"
    "/sources — список источников и их здоровье\n"
    "/last — последние отправленные\n"
    "/digest — собрать и прислать прямо сейчас\n"
    "/pause — пауза до /resume\n"
    "/resume — снять паузу\n"
    "/mute 2 — тишина 2 часа\n"
    "/help — эта справка"
)


class Runner:
    def __init__(self, tg, store, sources, cfg, llm=None, verbose=True):
        self.tg = tg
        self.store = store
        self.sources = sources
        self.cfg = cfg
        self.llm = llm
        self.verbose = verbose
        self.chat_id = store.kv_get("chat_id")

        self._stop = threading.Event()
        self._worker = None
        self._worker_store = None
        self._last_stats = None
        self._fetch_lock = threading.Lock()
        self.next_fetch = 0.0
        self.next_fast = 0.0

    # ------------------------------------------------------------------ utils
    def log(self, message):
        if self.verbose:
            stamp = datetime.now().strftime("%H:%M:%S")
            print("[%s] %s" % (stamp, message), flush=True)

    def is_paused(self):
        if self.store.kv_get("paused") == "1":
            return True
        muted = self.store.kv_get("muted_until")
        if muted:
            try:
                until = datetime.fromisoformat(muted)
            except ValueError:
                return False
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < until
        return False

    def fast_sources(self):
        """Sources worth checking every couple of minutes.

        Kept deliberately small: these are polled ~40x more often than the rest,
        and only pay off for feeds that actually break news.
        """
        return [s for s in self.sources
                if s.get("tier") == "fast" and s.get("enabled", True)]

    # --------------------------------------------------------------- fetching
    def _collect(self, store, sources, label, fast=False):
        if not sources:
            return None
        with self._fetch_lock:
            started = time.time()
            stats = pipeline.collect(store, sources, self.cfg, verbose=False)
            pipeline.rerank_pending(store, self.sources, self.cfg, self.llm,
                                    verbose=False, fast=fast)
            took = time.time() - started
        if stats["new"] or stats["failed"]:
            self.log("%s: new=%d ok=%d failed=%d unchanged=%d (%.1fs)"
                     % (label, stats["new"], stats["ok"], stats["failed"],
                        stats.get("skipped_unchanged", 0), took))
        self._last_stats = stats
        return stats

    def _worker_loop(self):
        """Collection runs here so the Telegram loop never waits on the network."""
        store = self._worker_store
        fast_interval = float(self.cfg.get("fast_interval_seconds", 90))
        full_interval = float(self.cfg.get("fetch_interval_seconds", 900))
        fast = self.fast_sources()

        while not self._stop.is_set():
            try:
                now = time.time()
                if now >= self.next_fetch:
                    self.next_fetch = now + full_interval
                    self.next_fast = now + fast_interval
                    self._collect(store, self.sources, "full fetch")
                    store.expire_pending(float(self.cfg.get("queue_ttl_hours", 6)))
                    store.prune_stories()
                elif fast and now >= self.next_fast:
                    self.next_fast = now + fast_interval
                    self._collect(store, fast, "fast lane", fast=True)
            except Exception:
                self.log("fetch worker error:\n%s" % traceback.format_exc())
                self._stop.wait(30)
            self._stop.wait(2)

    def start_worker(self):
        # Schedule the first cycles before the thread exists, or the worker
        # would start a full sweep at the same moment as the opening blocking
        # one and both would crawl the same 50 sources.
        now = time.time()
        self.next_fetch = now + float(self.cfg.get("fetch_interval_seconds", 900))
        self.next_fast = now + float(self.cfg.get("fast_interval_seconds", 90))
        self._worker = threading.Thread(target=self._worker_loop,
                                        name="fetch", daemon=True)
        self._worker.start()

    def fetch_now(self):
        """Blocking collection, for /digest and the first run only."""
        return self._collect(self._worker_store or self.store, self.sources,
                             "fetch on demand")

    # -------------------------------------------------------------- delivering
    def maybe_deliver(self, force=False):
        if not self.chat_id:
            return 0
        if self.is_paused() and not force:
            return 0
        chosen = pipeline.select_due(self.store, self.sources, self.cfg, force=force)
        if not chosen:
            return 0
        sent = pipeline.deliver(self.tg, self.store, self.chat_id, chosen, self.cfg,
                                verbose=self.verbose)
        if sent:
            breaking = len([c for c in chosen if c[1]])
            self.log("delivered %d item(s)%s"
                     % (sent, " (%d breaking)" % breaking if breaking else ""))
        return sent

    # --------------------------------------------------------------- updates
    def _authorized(self, chat_id):
        """Only the owner drives the bot.

        Anyone who finds the bot's name can open a chat with it; without this
        check a stranger could pause it, mute it for a day, or burn API credit
        with /digest.
        """
        if not self.chat_id:
            return True  # first contact claims ownership
        return str(chat_id) == str(self.chat_id)

    def handle_message(self, message):
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id:
            return
        if not self._authorized(chat_id):
            self.log("ignored message from chat_id=%s (not the owner)" % chat_id)
            try:
                self.tg.send_message(
                    chat_id, "Это личный бот. Он отвечает только своему владельцу.")
            except Exception:
                pass
            return
        if not self.chat_id:
            self.chat_id = chat_id
            self.store.kv_set("chat_id", chat_id)
            self.log("learned chat_id=%s" % chat_id)
            # First contact: do not dump the backlog that accumulated before the
            # operator was reachable.
            if not self.store.kv_get("bootstrap_done"):
                pipeline.bootstrap(
                    self.store, self.tg, chat_id, self.sources,
                    {"sources": len(self.sources), "ok": 0, "fetched": 0},
                    self.cfg, verbose=self.verbose)
                self.next_fetch = 0.0

        command = text.split()[0].split("@")[0].lower() if text else ""
        if command in ("/start", "/help"):
            self.tg.send_message(chat_id, "Привет. Я присылаю новости по ИИ и разработке.\n\n" + HELP)
        elif command == "/status":
            self.tg.send_message(chat_id, render.status_text(self.store, self.cfg, chat_id,
                                                             fast=len(self.fast_sources())))
        elif command == "/sources":
            self.tg.send_message(chat_id, render.sources_text(self.store, self.sources))
        elif command == "/last":
            rows = self.store.recent_sent(10)
            if not rows:
                self.tg.send_message(chat_id, "Пока ничего не отправлял.")
            else:
                for row in rows:
                    verdict = pipeline.get_verdict(self.store, row["nid"])
                    self.tg.send_message(
                        chat_id, render.item_card(row, verdict=verdict),
                        keyboard=render.feedback_keyboard(row["nid"], row.get("url")))
        elif command == "/digest":
            self.tg.send_message(chat_id, "Собираю свежее…")
            stats = self.fetch_now()
            sent = self.maybe_deliver(force=True)
            if not sent:
                self.tg.send_message(
                    chat_id, "Нового выше порога за последние %s ч нет. Собрал записей: %s."
                    % (self.cfg.get("max_age_hours", 6), (stats or {}).get("fetched", 0)))
        elif command == "/pause":
            self.store.kv_set("paused", "1")
            self.tg.send_message(chat_id, "Пауза. /resume чтобы возобновить.")
        elif command == "/resume":
            self.store.kv_set("paused", "0")
            self.store.kv_set("muted_until", "")
            self.tg.send_message(chat_id, "Возобновил.")
        elif command == "/mute":
            parts = text.split()
            hours = 2.0
            if len(parts) > 1:
                try:
                    hours = max(0.05, float(parts[1].replace(",", ".")))
                except ValueError:
                    hours = 2.0
            until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
            self.store.kv_set("muted_until", until)
            self.tg.send_message(chat_id, "Тишина на %.1f ч." % hours)
        else:
            self.tg.send_message(chat_id, HELP)

    def handle_callback(self, callback):
        data = callback.get("data") or ""
        callback_id = callback.get("id")
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        if chat_id and not self._authorized(chat_id):
            self.tg.answer_callback(callback_id, "Это личный бот.")
            return
        parts = data.split("|")
        if len(parts) != 3 or parts[0] != "fb":
            self.tg.answer_callback(callback_id, "Не понял команду")
            return
        verdict = "good" if parts[1] == "g" else "bad"
        try:
            nid = int(parts[2])
        except ValueError:
            self.tg.answer_callback(callback_id, "Битая ссылка на новость")
            return

        row = self.store.item(nid)
        if not row:
            self.tg.answer_callback(callback_id, "Новость не найдена")
            return
        if row.get("feedback"):
            self.tg.answer_callback(callback_id, "Уже оценено: %s"
                                    % render.VERDICT_LABEL.get(row["feedback"], ""))
            return

        # Terms are recomputed here rather than read from the verdict: the
        # heuristic keyword set is what the term weights are keyed by, and
        # without this the keyword half of the learning loop never fired.
        from . import score as scoring

        blob = "%s %s" % (row.get("title") or "", row.get("summary") or "")
        keywords = list(scoring._keyword_hits(blob.lower()).keys())
        self.store.set_feedback(nid, verdict, keywords)

        # learn: nudge this source and the terms the model/heuristic reacted to
        delta = 0.25 if verdict == "good" else -0.35
        self.store.bump_weight("source", row["source"], delta)
        for term in keywords[:8]:
            self.store.bump_weight("kw", term, delta * 0.6)

        self.tg.answer_callback(
            callback_id, "Спасибо, учёл" if verdict == "good" else "Понял, буду резать такое")

        if chat_id and message_id:
            original = (message.get("text") or "")
            label = render.VERDICT_LABEL.get(verdict, verdict)
            try:
                self.tg.edit_text(chat_id, message_id,
                                  render.rated_text(original, label), keyboard=None)
            except Exception:
                pass
        self.log("feedback nid=%s verdict=%s source=%s" % (nid, verdict, row["source"]))

    def poll_updates(self):
        offset = self.store.kv_get("update_offset")
        offset = int(offset) if offset else None
        try:
            updates = self.tg.get_updates(offset=offset, timeout=int(
                self.cfg.get("poll_timeout_seconds", 25)))
        except Exception as exc:
            self.log("getUpdates failed: %s" % exc)
            time.sleep(5)
            return
        for update in updates or []:
            offset = update["update_id"] + 1
            try:
                if "message" in update:
                    self.handle_message(update["message"])
                elif "callback_query" in update:
                    self.handle_callback(update["callback_query"])
            except Exception:
                self.log("update handling failed:\n%s" % traceback.format_exc())
            # Persist per update: a crash mid-batch used to replay the whole
            # batch, including the commands already acted on.
            self.store.kv_set("update_offset", offset)

    # ------------------------------------------------------------------- loop
    def run(self):
        fast = self.fast_sources()
        self.log("runner started; chat_id=%s; llm=%s; sources=%d (fast lane: %d)"
                 % (self.chat_id or "not learned yet",
                    "on" if (self.llm and self.llm.enabled) else "off (heuristic only)",
                    len(self.sources), len(fast)))
        self.log("live: fast lane every %ss, full sweep every %ss, max age %sh"
                 % (self.cfg.get("fast_interval_seconds", 90),
                    self.cfg.get("fetch_interval_seconds", 900),
                    self.cfg.get("max_age_hours", 6)))
        # One blocking sweep first, so /digest-worthy state exists immediately
        # and bootstrap has real numbers; the worker takes over after that.
        self._worker_store = Store(self.store.path)
        stats = self.fetch_now()
        self.start_worker()
        if stats and self.chat_id:
            pipeline.bootstrap(self.store, self.tg, self.chat_id, self.sources,
                               stats, self.cfg, verbose=self.verbose)
        try:
            while True:
                try:
                    self.maybe_deliver()
                    self.poll_updates()
                except KeyboardInterrupt:
                    raise
                except Exception:
                    self.log("loop error:\n%s" % traceback.format_exc())
                    time.sleep(10)
        except KeyboardInterrupt:
            self.log("stopped by user")
        finally:
            self._stop.set()
            if self._worker:
                self._worker.join(timeout=5)
