"""Checks for the calendar, accumulation and event-pooling logic.

These run on synthetic arrays, so they can be run before any CHIRPS data has landed.

    <crop-python> test_engine.py
"""
import datetime as dt

import numpy as np

import config as cfg
import detect as D
import series as S


def check(name, cond, detail=""):
    print("%-46s %s %s" % (name, "PASS" if cond else "FAIL", detail))
    return bool(cond)


def main():
    ok = []

    # ---- pseudo day-of-year -------------------------------------------------
    d = [dt.date(2019, 3, 1), dt.date(2020, 3, 1), dt.date(2020, 2, 29),
         dt.date(2020, 2, 28), dt.date(2019, 12, 31), dt.date(2020, 12, 31)]
    p = S.pseudo_doy(d)
    ok.append(check("1 Mar aligns across leap/common years", p[0] == p[1], "%d==%d" % (p[0], p[1])))
    ok.append(check("29 Feb shares the 28 Feb slot", p[2] == p[3] == 59, str(p[2:4])))
    ok.append(check("31 Dec is 365 in both year types", p[4] == p[5] == 365, str(p[4:])))

    # ---- week bins ----------------------------------------------------------
    w = S.week_of_year(np.array([1, 7, 8, 364, 365]))
    ok.append(check("week bins 1,1,2,52,52", list(w) == [1, 1, 2, 52, 52], str(list(w))))

    # ---- rolling sum --------------------------------------------------------
    x = np.arange(60, dtype="float32").reshape(-1, 1)
    r = S.rolling_sum(x, 30)
    ok.append(check("rolling sum warm-up is NaN", np.isnan(r[:29, 0]).all()))
    ok.append(check("rolling sum value correct", abs(r[29, 0] - x[:30, 0].sum()) < 1e-3,
                    "%.1f" % r[29, 0]))
    ok.append(check("rolling sum slides correctly",
                    abs(r[30, 0] - x[1:31, 0].sum()) < 1e-3, "%.1f" % r[30, 0]))
    xn = x.copy(); xn[10] = np.nan
    rn = S.rolling_sum(xn, 30)
    # index 39 still sees the gap (window 10-39); index 40 is the first clean window
    ok.append(check("every window containing the gap is NaN",
                    np.isnan(rn[29:40, 0]).all()))
    ok.append(check("first window past the gap recovers", np.isfinite(rn[40, 0]),
                    "%.1f" % rn[40, 0]))

    # ---- circular smoothing -------------------------------------------------
    a = np.zeros((365, 1)); a[0] = 365.0
    s = S.circular_smooth(a, 31)
    ok.append(check("circular smooth wraps year end", s[-1, 0] > 0 and s[0, 0] > 0,
                    "%.2f %.2f" % (s[-1, 0], s[0, 0])))
    ok.append(check("circular smooth conserves mass",
                    abs(s.sum() - a.sum()) < 1e-6, "%.4f" % (s.sum() - a.sum())))

    # ---- run detection ------------------------------------------------------
    f = np.array([0, 1, 1, 0, 0, 1, 0], bool)
    ok.append(check("runs_of_true finds both runs", D.runs_of_true(f) == [(1, 2), (5, 5)]))
    ok.append(check("runs_of_true handles edges",
                    D.runs_of_true(np.array([1, 1, 0, 1], bool)) == [(0, 1), (3, 3)]))
    ok.append(check("runs_of_true on all-false", D.runs_of_true(np.zeros(5, bool)) == []))

    # ---- pooling ------------------------------------------------------------
    n = 100
    deficit = np.zeros(n); surplus = np.zeros(n)
    deficit[0:30] = 1.0                       # 30 mm of deficit
    surplus[30:35] = 0.5                      # 2.5 mm back = 8% of it
    deficit[35:60] = 1.0
    runs = [(0, 29), (35, 59)]
    m = D.pool_runs(runs, deficit, surplus)
    ok.append(check("short weak gap merges the two runs", m == [[0, 59]], str(m)))

    surplus2 = surplus.copy(); surplus2[30:35] = 3.0     # 15 mm back = 50% of it
    m2 = D.pool_runs(runs, deficit, surplus2)
    ok.append(check("a properly wet gap keeps them apart", m2 == [[0, 29], [35, 59]], str(m2)))

    deficit3 = np.zeros(140); deficit3[0:30] = 1.0; deficit3[75:110] = 1.0
    runs3 = [(0, 29), (75, 109)]                         # 45-day gap > POOL_GAP_DAYS
    m3 = D.pool_runs(runs3, deficit3, np.zeros(140))
    ok.append(check("gap longer than the pooling window splits",
                    m3 == [[0, 29], [75, 109]], str(m3)))

    # ---- end-to-end on a synthetic basin ------------------------------------
    dates = [dt.date(2019, 1, 1) + dt.timedelta(days=i) for i in range(730)]
    nb = 1
    rain = np.full((730, nb), 5.0, "float32")            # 150 mm/30 d everywhere
    rain[120:260] = 0.2                                  # a long dry spell in 2019
    rain[180:188] = 14.0                                 # punctuated by a wet week that
                                                         # briefly lifts P30 over the threshold
    p30 = S.rolling_sum(rain, 30)
    thr20 = np.full((365, nb), 100.0, "float32")         # threshold 100 mm/30 d
    thr10 = np.full((365, nb), 60.0, "float32")
    ass = np.ones((365, nb), bool)
    ev, in_dr, _ = D.detect_events(dates, p30, thr20, thr10, ass)
    raw_runs = D.runs_of_true(in_dr[:, 0])
    ok.append(check("the wet week does split the raw runs", len(raw_runs) == 2, str(raw_runs)))
    ok.append(check("pooling puts it back into ONE event", len(ev) == 1, str(len(ev))))
    if len(ev):
        ok.append(check("wet days recorded as absorbed",
                        ev.wet_days_absorbed.iloc[0] > 0, str(ev.wet_days_absorbed.iloc[0])))
        ok.append(check("duration in the right ballpark (~140 d)",
                        120 <= ev.duration_days.iloc[0] <= 185, str(ev.duration_days.iloc[0])))

    # a drought-breaking rain must NOT be pooled away
    rain_break = rain.copy()
    rain_break[180:200] = 40.0                           # 800 mm, a genuine wet spell
    ev3, _, _ = D.detect_events(dates, S.rolling_sum(rain_break, 30), thr20, thr10, ass)
    ok.append(check("a real drought-breaking rain splits the event",
                    len(ev3) == 2, str(len(ev3))))

    # a basin that is merely seasonally dry must NOT be flagged
    thr_dry = np.full((365, nb), 1.0, "float32")
    ass_dry = thr_dry >= cfg.MIN_THR_MM
    rain_dry = np.full((730, nb), 0.0, "float32")
    ev2, _, _ = D.detect_events(dates, S.rolling_sum(rain_dry, 30), thr_dry, thr_dry, ass_dry)
    ok.append(check("climatologically rainless season not flagged", len(ev2) == 0, str(len(ev2))))

    print("\n%d/%d checks passed" % (sum(ok), len(ok)))
    return 0 if all(ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
