#!/usr/bin/env python3
"""ai-news-bot — personal AI/tech news bot for Telegram.

Zero third-party dependencies: runs on a bare Python 3.9+ interpreter.

    python3 bot.py whoami      # show chat ids that messaged the bot
    python3 bot.py check       # validate every source and print what it returns
    python3 bot.py once        # one fetch+deliver cycle, then exit (cron mode)
    python3 bot.py once --dry-run --top 12   # show what WOULD be sent, send nothing
    python3 bot.py run         # live loop: fetch + deliver + buttons + commands
    python3 bot.py models      # list DeepSeek model ids for your API key
    python3 bot.py explain "some headline"   # show how the scorer rates a title
    python3 bot.py test-send   # send one test message to the stored chat
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from newsbot import pipeline, render, score as scoring  # noqa: E402
from newsbot.llm import DeepSeek, LLMError  # noqa: E402
from newsbot.runner import Runner, HELP  # noqa: E402
from newsbot.store import Store  # noqa: E402
from newsbot.telegram import Telegram, TelegramError  # noqa: E402


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

def load_env(path=None):
    path = path or os.path.join(HERE, ".env")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_json(path, default=None):
    if not os.path.isfile(path):
        if default is None:
            raise SystemExit("missing file: %s" % path)
        return default
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def paths(args):
    return (
        os.path.join(HERE, args.sources) if not os.path.isabs(args.sources)
        else args.sources,
        os.path.join(HERE, args.config) if not os.path.isabs(args.config)
        else args.config,
        os.path.join(HERE, args.db) if not os.path.isabs(args.db) else args.db,
    )


def build(args, need_telegram=False):
    load_env()
    sources_path, config_path, db_path = paths(args)
    cfg = load_json(config_path, {})
    for key, value in (cfg.get("settings") or {}).items():
        cfg.setdefault(key, value)
    sources = load_json(sources_path, {"sources": []})["sources"]
    sources = [s for s in sources if s.get("enabled", True) or args.include_disabled]
    store = Store(db_path)
    tg = None
    if need_telegram or os.environ.get("TELEGRAM_BOT_TOKEN"):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if token:
            tg = Telegram(token)
        elif need_telegram:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env)")
    llm = DeepSeek(store=store, profile=(cfg.get("interests") or {}).get("profile"))
    return sources, cfg, store, tg, llm


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_whoami(args):
    _, _, store, tg, _ = build(args, need_telegram=True)
    print("bot:", tg.get_me().get("username"))
    updates = tg.get_updates(offset=None, timeout=0)
    if not updates:
        print("\nПока никто не писал боту. Открой бота в Telegram и нажми Start,")
        print("затем повтори: python3 bot.py whoami")
        return 0
    seen = {}
    for update in updates:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = chat.get("username") or chat.get("first_name") or "?"
    for chat_id, who in seen.items():
        print("chat_id=%s  (%s)" % (chat_id, who))
    store.kv_set("chat_id", list(seen)[-1])
    print("\nsaved chat_id=%s to state" % list(seen)[-1])
    return 0


def cmd_check(args):
    sources, cfg, store, _, _ = build(args)
    # x_miner reads previously stored Telegram posts, so it needs the store the
    # same way a real collect cycle gives it; without this `check` reports a
    # working source as broken.
    from newsbot import x_sources
    x_sources.set_store(store)
    targets = [s for s in sources if not args.source or s["name"] == args.source]
    print("checking %d sources\n" % len(targets))
    ok = 0
    rows = []
    for source in targets:
        started = time.time()
        try:
            items, error, _ = pipeline.fetch_source(source)
        except Exception as exc:
            items, error = [], "%s: %s" % (type(exc).__name__, exc)
        took = time.time() - started
        if error:
            rows.append((source["name"], source.get("type"), 0, "", took, error))
            print("  FAIL  %-24s %-18s %s" % (source["name"], source.get("type"), error[:90]))
            continue
        ok += 1
        newest = max((i.get("published") or "" for i in items), default="")
        rows.append((source["name"], source.get("type"), len(items), newest, took, ""))
        print("  ok    %-24s %-18s %3d items  newest=%s  (%.1fs)"
              % (source["name"], source.get("type"), len(items), newest[:19] or "n/a", took))
    print("\n%d/%d sources responded" % (ok, len(targets)))
    return 0


def cmd_once(args):
    sources, cfg, store, tg, llm = build(args, need_telegram=not args.dry_run)
    if not args.dry_run and not store.kv_get("chat_id"):
        raise SystemExit("chat_id is unknown — send /start to the bot, run whoami")

    print("fetching…" if not args.no_fetch else "skipping fetch (--no-fetch)")
    stats = {"fetched": 0, "new": 0, "ok": 0, "failed": 0, "errors": {}}
    if not args.no_fetch:
        stats = pipeline.collect(store, sources, cfg, verbose=True)
        print("fetched=%d new=%d ok=%d failed=%d" % (
            stats["fetched"], stats["new"], stats["ok"], stats["failed"]))
        if stats["errors"]:
            for name, error in list(stats["errors"].items())[:10]:
                print("  error %s: %s" % (name, error))
    print("scoring…")
    rank = pipeline.rerank_pending(store, sources, cfg, llm, verbose=True)
    print("scanned=%d judged=%d llm_calls=%d" % (
        rank["scanned"], rank["judged"], rank["llm_calls"]))

    if args.dry_run:
        pending = store.pending(limit=200)
        pending.sort(key=lambda r: pipeline.effective_score(r, store, cfg), reverse=True)
        print("\n=== top %d candidates (nothing sent) ===" % args.top)
        for row in pending[:args.top]:
            verdict = pipeline.get_verdict(store, row["nid"])
            value = pipeline.effective_score(row, store, cfg)
            stats = store.story_stats(row.get("story_id"))
            base = pipeline.base_score(row, store, cfg, verdict=verdict)
            breaking = pipeline.is_breaking_now(
                value, base, row, cfg, {}, stats,
                pipeline.item_age_hours(row), verdict)
            mark = "BREAK" if breaking else "     "
            print("\n%s %.2f  [%s] %s" % (mark, value, row["source"], row["title"][:88]))
            if verdict:
                print("        llm: %s (%s) %s" % (verdict.get("score"),
                                                    verdict.get("category"),
                                                    verdict.get("reason", "")[:60]))
                if verdict.get("summary_ru"):
                    print("        ru: %s" % verdict["summary_ru"][:150])
            print("        %s" % (row.get("url") or ""))
        print("\n(candidates above)")
        print("\n=== would be sent RIGHT NOW (budget + dedupe applied, nothing marked) ===")
        selection = pipeline.select_due(store, sources, cfg, force=True, dry=True)
        if not selection:
            print("  (nothing above the threshold right now)")
        for value, breaking, row in selection:
            print("  %s %.2f  [%s] %s" % ("BREAK" if breaking else "     ", value,
                                          row["source"], row["title"][:80]))
        return 0

    store.expire_pending(float(cfg.get("queue_ttl_hours", 8)))
    if pipeline.bootstrap(store, tg, store.kv_get("chat_id"), sources, stats, cfg,
                          verbose=True):
        return 0
    chosen = pipeline.select_due(store, sources, cfg)
    sent = pipeline.deliver(tg, store, store.kv_get("chat_id"), chosen, cfg, verbose=True)
    print("delivered=%d" % sent)
    return 0


def cmd_run(args):
    sources, cfg, store, tg, llm = build(args, need_telegram=True)
    runner = Runner(tg, store, sources, cfg, llm=llm, verbose=not args.quiet)
    try:
        runner.tg.set_commands([
            ("status", "состояние и лимиты"),
            ("sources", "источники"),
            ("last", "последние новости"),
            ("digest", "собрать и прислать сейчас"),
            ("pause", "пауза"),
            ("resume", "возобновить"),
            ("mute", "тишина N часов"),
            ("help", "справка"),
        ])
    except TelegramError:
        pass
    try:
        runner.run()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_sources(args):
    sources, _, store, _, _ = build(args)
    print(render.sources_text(store, sources).replace("<b>", "").replace("</b>", ""))
    return 0


def cmd_stats(args):
    _, cfg, store, _, llm = build(args)
    print(json.dumps(store.counts(), ensure_ascii=False, indent=2))
    print("\nsource health:")
    for row in store.source_health():
        print("  %-24s ok=%-4s fail=%-3s %s" % (
            row["name"], row["ok_count"], row["fail_count"], row["last_error"] or ""))
    print("\nlearned weights:")
    print(json.dumps(store.weights(), ensure_ascii=False, indent=2))
    print("\nllm usage today: %s" % json.dumps(llm.usage_today(), ensure_ascii=False))
    print("llm enabled: %s (model=%s base=%s)" % (llm.enabled, llm.model, llm.base_url))
    return 0


def cmd_models(args):
    _, _, store, _, llm = build(args)
    if not llm.enabled:
        print("DEEPSEEK_API_KEY is not set — add it to .env")
        return 2
    try:
        for model_id in llm.models():
            marker = "  <- configured" if model_id == llm.model else ""
            print("  %s%s" % (model_id, marker))
    except LLMError as exc:
        print("failed: %s" % exc)
        return 1
    return 0


def cmd_explain(args):
    _, cfg, _, _, _ = build(args)
    sources = load_json(os.path.join(HERE, args.sources), {"sources": []})["sources"]
    item = {"title": args.title, "summary": args.summary or "", "source": args.source,
            "published": None, "extra": {}}
    source_cfg = next((s for s in sources if s["name"] == args.source), {"weight": 1.0})
    print(scoring.explain(item, source_cfg, {}, cfg=cfg))
    return 0


def cmd_xdiag(args):
    """Probe every X discovery route and the content cascade, with next steps."""
    import re

    _, _, _, _, _ = build(args)
    from newsbot import x_sources
    from newsbot.http import fetch

    store = Store(os.path.join(HERE, args.db) if not os.path.isabs(args.db) else args.db)
    handle = args.handle
    print("X-диагностика для @%s\n" % handle)
    print("  %-22s %-10s %s" % ("МАРШРУТ ОБНАРУЖЕНИЯ", "СТАТУС", "ДЕТАЛИ"))
    sample_id = None
    for template, headers, name in x_sources.DISCOVERY_ROUTES:
        url = template % handle
        result = fetch(url, extra_headers=headers, retries=0, timeout=15)
        if not result.ok:
            print("  %-22s %-10s %s" % (name, "нет", str(result.error or result.status)[:60]))
            continue
        text = result.text()
        if "not yet whitelist" in text.lower():
            match = re.search(r"this ID:\s*([0-9a-f]{40,})", text)
            print("  %-22s %-10s нужна вайтлиста" % (name, "RSS ок"))
            print("      ID для письма: %s" % ((match.group(1)[:64] + "...") if match else "не найден"))
            continue
        try:
            posts = x_sources.parse_status_feed(result.body, handle)
        except Exception as exc:
            print("  %-22s %-10s %s" % (name, "не фид", str(exc)[:60]))
            continue
        if posts:
            sample_id = sample_id or posts[0]["tweet_id"]
            print("  %-22s %-10s постов=%d, свежайший=%s"
                  % (name, "ok", len(posts), posts[0].get("published") or "?"))
        else:
            print("  %-22s %-10s фид пуст" % (name, "жив"))

    print("\n  %-22s %-10s %s" % ("СЛОЙ КОНТЕНТА", "СТАТУС", "ДЕТАЛИ"))
    if not sample_id:
        # Discovery can be entirely blocked while content still works; fall back
        # to the newest post we already mined so the content cascade is verified.
        try:
            row = store.db.execute(
                "SELECT tweet_id, handle FROM x_links ORDER BY tweet_id DESC LIMIT 1"
            ).fetchone()
            if row:
                sample_id = row["tweet_id"]
                handle = row["handle"] or handle
        except Exception:
            pass
    if args.tweet_id or sample_id:
        tweet_id = args.tweet_id or sample_id
        parsed, route, error = x_sources.content(tweet_id, handle)
        if parsed:
            print("  %-22s %-10s маршрут=%s, автор=@%s"
                  % ("по ID " + tweet_id[-6:], "ok", route, parsed.get("author")))
            print("      текст: %s" % (parsed.get("text") or "")[:70].replace("\n", " "))
        else:
            print("  %-22s %-10s %s" % ("по ID " + tweet_id[-6:], "нет", str(error)[:60]))
    else:
        print("  (нет ни одного ID для проверки — сначала нужен рабочий маршрут)")

    print("\nЧТО ДЕЛАТЬ:")
    print(" 1. xcancel: одно письмо на rss@xcancel.com — попросить вайтлисту ридера,")
    print("    приложив ID выше. Дальше это обычный стабильный RSS-маршрут.")
    print(" 2. twiiit: работает, но режет по IP при частых запросах — держим паузу")
    print("    x_politeness_delay_seconds и cooldown на маршрут.")
    print(" 3. x_miner: ссылки на посты X ищутся в уже скачанных страницах Telegram —")
    print("    запросов к X ноль. Смотреть: python3 bot.py stats")
    return 0


def cmd_test_send(args):
    _, _, store, tg, _ = build(args, need_telegram=True)
    chat_id = store.kv_get("chat_id")
    if not chat_id:
        raise SystemExit("chat_id unknown — send /start to the bot and run whoami")
    # Buttons point at a real, already-delivered item: with a dummy id they
    # would answer "not found" and prove nothing about the feedback path.
    row = store.db.execute(
        "SELECT nid, title, url FROM items WHERE status='sent' AND feedback IS NULL "
        "ORDER BY sent_at DESC LIMIT 1").fetchone()
    if row:
        text = ("<b>Тест доставки</b>\nЕсли ты это видишь — доставка работает.\n\n"
                "Кнопки ниже настоящие: они оценивают последнюю неоценённую новость\n"
                "<i>%s</i>" % render.esc((row["title"] or "")[:90]))
        keyboard = render.feedback_keyboard(row["nid"], row["url"])
    else:
        text = ("<b>Тест доставки</b>\nЕсли ты это видишь — доставка работает.\n\n"
                "Неоценённых новостей сейчас нет, поэтому кнопки не прикреплены.")
        keyboard = None
    tg.send_message(chat_id, text, keyboard=keyboard)
    print("sent to chat_id=%s" % chat_id)
    return 0


# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(prog="bot.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sources", default="sources.json")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--db", default="state/news.db")
    parser.add_argument("--include-disabled", action="store_true")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("run", help="live loop")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("once", help="one cycle then exit")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-fetch", action="store_true",
                   help="skip fetching, only re-score and select")
    p.add_argument("--top", type=int, default=12)
    p.set_defaults(func=cmd_once)

    p = sub.add_parser("check", help="validate sources")
    p.add_argument("--source")
    p.set_defaults(func=cmd_check)

    sub.add_parser("whoami", help="learn the chat id").set_defaults(func=cmd_whoami)
    sub.add_parser("sources", help="list sources").set_defaults(func=cmd_sources)
    sub.add_parser("stats", help="state and learned weights").set_defaults(func=cmd_stats)
    sub.add_parser("models", help="list DeepSeek models").set_defaults(func=cmd_models)
    sub.add_parser("test-send", help="send a test message").set_defaults(func=cmd_test_send)

    p = sub.add_parser("explain", help="explain a score")
    p.add_argument("title")
    p.add_argument("--summary", default="")
    p.add_argument("--source", default="techcrunch_ai")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("xdiag", help="probe X discovery routes")
    p.add_argument("--handle", default="OpenAI")
    p.add_argument("--tweet-id", dest="tweet_id", default=None)
    p.set_defaults(func=cmd_xdiag)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except SystemExit:
        raise
    except (TelegramError, LLMError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
