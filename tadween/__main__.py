"""Command line: `python -m tadween serve` runs the app, `python -m tadween transcribe FILE` does one file."""
import argparse
import sys
from pathlib import Path


def _print_progress(stage, fraction):
    print(f"\r\033[K{stage}", end="", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(prog="tadween", description="Private, local call transcription with speaker names.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="start the app (default http://127.0.0.1:8765)")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    one = sub.add_parser("transcribe", help="transcribe one recording and write the transcript next to it")
    one.add_argument("file")
    one.add_argument("--speakers", type=int, help="how many people talk (default: detect)")
    one.add_argument("--format", default="txt", choices=["txt", "md", "srt", "vtt"])
    args = parser.parse_args()

    if args.cmd == "serve":
        from . import server
        server.run(args.port, open_browser=not args.no_browser)
        return

    from . import edits, pipeline, store
    src = Path(args.file).expanduser()  # the shell leaves a quoted ~ alone
    pipeline.on_progress = _print_progress
    pipeline.hold_data_lock()  # so an app started meanwhile leaves this run alone
    try:
        tid = pipeline.import_file(src, queue_job=False)
    except ValueError as e:
        sys.exit(str(e))
    try:
        pipeline.process(tid)
    except KeyboardInterrupt:
        store.delete(tid)  # cancelled: nothing half-done for the app to resume later
        raise
    if args.speakers:
        pipeline.regroup(tid, args.speakers)
    t = store.load(tid)
    print(file=sys.stderr)
    if t["status"] != "ready":
        sys.exit(f"Failed: {t.get('error')}")
    _, _, text = edits.export(t, args.format)
    out = src.with_name(f"{src.stem} - transcript.{args.format}")
    out.write_text(text, encoding="utf-8")
    names = ", ".join(s["name"] for s in t["speakers"].values())
    print(f"{len(t['turns'])} lines, speakers: {names}\nSaved {out}\nOpen the app to name speakers and fix words: ./tadween.sh")


if __name__ == "__main__":
    main()
