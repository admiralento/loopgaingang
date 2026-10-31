// LDO load-step settling analysis - set OUTPUT to choose which value this measurement shows:
//   "nominal" = Ch1 average before the step (V)
//   "peak"    = largest excursion after the step, dip or overshoot (V)
//   "band"    = settling band half-width, SETTLE_FRACTION x |peak - nominal| (V)
//   "upper"   = nominal + band (V)      "lower" = nominal - band (V)
//   "time"    = settling time: from the Ch2 edge until Ch1 stays inside the band (s)
//   "noise"   = RMS noise of smoothed Ch1 before the step (V) - should be well below "band"
//   "rate"    = sample rate the script is using (Hz)
var OUTPUT = "time"
var EDGE = "rising"           // "rising" or "falling" - must match the Scope trigger condition

var SETTLE_FRACTION = 0.1     // settled = within +/-10% of the peak deviation from nominal
var SMOOTH_TIME = 1e-6        // moving-average width (s) - keep well below the ringing period
var PEAK_WINDOW = 20e-6       // look for the peak within this long after the step (s)
var BLANK_TIME = 0.5e-6       // ignore the switching glitch this long either side of the edge (s)

var d = Scope.Channel1.data
var s = Scope.Channel2.data
var n = d.length
var up = (EDGE == "rising")

// 0. Sample rate from the Scope settings
var rate = NaN
try { rate = Scope.Time.Rate.value } catch (e) {}
if (!(rate > 0)) {
    try { rate = n / (10 * Scope.Time.Base.value) } catch (e) {}
}
var dt = 1 / rate

var v0 = NaN, vPk = NaN, band = NaN, noise = NaN, tSet = NaN

if (rate > 0) {
    // 1. Load step = Ch2 edge through its midpoint
    var sMin = s[0], sMax = s[0]
    for (var i = 1; i < n; i++) { if (s[i] < sMin) sMin = s[i]; if (s[i] > sMax) sMax = s[i] }
    var sMid = (sMin + sMax) / 2
    var iStep = -1
    for (var i = 1; i < n; i++) {
        if (up ? (s[i-1] < sMid && s[i] >= sMid) : (s[i-1] > sMid && s[i] <= sMid)) { iStep = i; break }
    }

    var nb = Math.max(1, Math.round(BLANK_TIME / dt))
    var iA = iStep - nb, iB = Math.min(n - 1, iStep + nb)

    if (iA > 10) {
        // 2. Nominal = mean of raw Ch1 before the step (excluding the glitch)
        var sum = 0
        for (var i = 0; i < iA; i++) sum += d[i]
        v0 = sum / iA

        // 3. Copy Ch1 and replace the glitch region with a straight line
        var x = new Array(n)
        for (var i = 0; i < n; i++) x[i] = d[i]
        for (var i = iA + 1; i < iB; i++) x[i] = d[iA] + (d[iB] - d[iA]) * (i - iA) / (iB - iA)

        // 4. Smooth with a centered moving average (prefix sums)
        var p = new Array(n + 1); p[0] = 0
        for (var i = 0; i < n; i++) p[i+1] = p[i] + x[i]
        var h = Math.max(1, Math.floor(SMOOTH_TIME / dt / 2))
        var f = new Array(n)
        for (var i = 0; i < n; i++) {
            var a = Math.max(0, i - h), b = Math.min(n, i + h + 1)
            f[i] = (p[b] - p[a]) / (b - a)
        }

        // 5. Noise = RMS of smoothed Ch1 around nominal, before the step
        var sq = 0, cnt = 0
        for (var i = h; i < iA - h; i++) { sq += (f[i] - v0) * (f[i] - v0); cnt++ }
        if (cnt > 0) noise = Math.sqrt(sq / cnt)

        // 6. Peak = largest deviation from nominal (dip or overshoot) after the step
        var iEnd = Math.min(n, iStep + Math.round(PEAK_WINDOW / dt))
        var iPk = iB
        for (var i = iB; i < iEnd; i++) {
            if (Math.abs(f[i] - v0) > Math.abs(f[iPk] - v0)) iPk = i
        }
        vPk = f[iPk]
        band = SETTLE_FRACTION * Math.abs(vPk - v0)

        // 7. Settling time = after the last sample outside the band
        var iStop = n - h              // ignore the end where the average is one-sided
        var iLast = -1
        for (var i = iStep; i < iStop; i++) {
            if (Math.abs(f[i] - v0) > band) iLast = i
        }
        if (iLast < 0) tSet = 0
        else if (iLast < iStop - 1) tSet = (iLast + 1 - iStep) * dt
        // else: still outside the band at the end of the capture -> NaN
    }
}

OUTPUT == "nominal" ? v0 :
OUTPUT == "peak"    ? vPk :
OUTPUT == "band"    ? band :
OUTPUT == "upper"   ? v0 + band :
OUTPUT == "lower"   ? v0 - band :
OUTPUT == "noise"   ? noise :
OUTPUT == "rate"    ? rate :
OUTPUT == "time"    ? tSet : NaN