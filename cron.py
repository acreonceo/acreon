"""
Scheduled refresh runner. Render Cron Jobs invoke this on a schedule.

  python cron.py signals   -> run all growth signals (cheap; monthly)
  python cron.py full      -> re-pull the whole county, then all signals (quarterly)

Needs DATABASE_URL and CENSUS_KEY in the environment (same values as the web service).
"""
import sys
import main

mode = sys.argv[1] if len(sys.argv) > 1 else "signals"
if mode == "full":
    main.run_full_refresh(parcels=True)
else:
    main.run_all_signals()

main.pool.close()
print("cron done:", mode, "->", main.SIGNAL_STATUS)
