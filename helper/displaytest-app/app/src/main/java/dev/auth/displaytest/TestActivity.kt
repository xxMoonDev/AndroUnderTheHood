package dev.auth.displaytest

import android.app.Activity
import android.content.Intent
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Shader
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.util.Log
import android.view.Choreographer
import android.view.MotionEvent
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.WindowManager

/**
 * Full-screen display & touch test, driven by AndroUnderTheHood:
 *   am start -n dev.auth.displaytest/.TestActivity --es mode <mode> [--ez max_brightness true]
 * modes: color:#RRGGBB | sweep | gradient | grid | checker | touch | fps
 * Results go to logcat tag "AuthDisplayTest" as one JSON object per line.
 */
class TestActivity : Activity() {
    companion object {
        const val TAG = "AuthDisplayTest"
        val SWEEP = intArrayOf(Color.RED, Color.GREEN, Color.BLUE, Color.WHITE, Color.BLACK,
            Color.rgb(128, 128, 128), Color.CYAN, Color.MAGENTA, Color.YELLOW)
    }

    private lateinit var view: TestView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        // started from the PC: show over the lock screen and switch the screen on (only draws test patterns)
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        if (Build.VERSION.SDK_INT >= 28) {
            window.attributes = window.attributes.apply {
                layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
            }
        }
        view = TestView(this)
        setContentView(view)
        apply(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        apply(intent)
    }

    private fun apply(i: Intent) {
        val lp = window.attributes
        lp.screenBrightness = if (i.getBooleanExtra("max_brightness", false)) 1f
            else WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE
        window.attributes = lp
        if (Build.VERSION.SDK_INT >= 30) {
            window.insetsController?.let {
                it.hide(WindowInsets.Type.statusBars() or WindowInsets.Type.navigationBars())
                it.systemBarsBehavior = WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            @Suppress("DEPRECATION")
            view.systemUiVisibility = View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
        }
        view.setMode(i.getStringExtra("mode") ?: "sweep")
    }

    override fun onDestroy() {
        view.stop()
        super.onDestroy()
    }

    inner class TestView(ctx: Activity) : View(ctx), Choreographer.FrameCallback {
        private var mode = "sweep"
        private var color = Color.BLACK
        private var sweepIdx = 0
        private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
        private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.GRAY; textSize = 36f }

        // touch
        private val cols = 9
        private val rows = 16
        private var covered = BooleanArray(cols * rows)
        private val paths = HashMap<Int, Path>()
        private val done = ArrayList<Pair<Path, Int>>()
        private var maxPointers = 0
        private var events = 0
        private var firstEvt = 0L
        private var lastReport = 0L
        private val pointerColors = intArrayOf(Color.RED, Color.GREEN, Color.CYAN, Color.YELLOW, Color.MAGENTA,
            Color.WHITE, Color.rgb(255, 128, 0), Color.rgb(0, 128, 255), Color.rgb(160, 255, 160), Color.LTGRAY)

        // fps
        private var frames = 0
        private var fpsStart = 0L
        private var lastFrame = 0L
        private var minInt = Long.MAX_VALUE
        private var maxInt = 0L
        private var running = false
        private var phase = 0f

        fun setMode(m: String) {
            mode = m
            stop()
            when {
                m.startsWith("color:") -> color = Color.parseColor(m.removePrefix("color:"))
                m == "sweep" -> { sweepIdx = 0; color = SWEEP[0] }
                m == "touch" -> {
                    covered = BooleanArray(cols * rows); paths.clear(); done.clear()
                    maxPointers = 0; events = 0; firstEvt = 0
                }
                m == "fps" -> {
                    frames = 0; fpsStart = 0; lastFrame = 0; minInt = Long.MAX_VALUE; maxInt = 0
                    running = true
                    Choreographer.getInstance().postFrameCallback(this)
                }
            }
            Log.i(TAG, "{\"mode\":\"$m\",\"w\":$width,\"h\":$height}")
            invalidate()
        }

        fun stop() {
            running = false
            Choreographer.getInstance().removeFrameCallback(this)
        }

        override fun doFrame(frameTimeNanos: Long) {
            if (!running) return
            if (lastFrame != 0L) {
                val d = frameTimeNanos - lastFrame
                if (d < minInt) minInt = d
                if (d > maxInt) maxInt = d
            } else fpsStart = frameTimeNanos
            lastFrame = frameTimeNanos
            frames++
            phase = (phase + 6f) % 360f
            invalidate()
            val el = frameTimeNanos - fpsStart
            if (el >= 2_000_000_000L && frames > 2) {
                val fps = (frames - 1) * 1e9 / el
                Log.i(TAG, "{\"fps\":%.1f,\"min_ms\":%.2f,\"max_ms\":%.2f,\"frames\":%d}"
                    .format(fps, minInt / 1e6, maxInt / 1e6, frames))
                frames = 0; fpsStart = frameTimeNanos; minInt = Long.MAX_VALUE; maxInt = 0; lastFrame = frameTimeNanos
            }
            Choreographer.getInstance().postFrameCallback(this)
        }

        override fun onDraw(c: Canvas) {
            val w = width.toFloat()
            val h = height.toFloat()
            when {
                mode.startsWith("color:") || mode == "sweep" -> {
                    c.drawColor(color)
                    if (mode == "sweep") c.drawText("tap for next colour", 24f, h - 30f,
                        text.apply { this.color = if (color == Color.WHITE || color == Color.YELLOW) Color.DKGRAY else Color.GRAY })
                }
                mode == "gradient" -> {
                    val bands = arrayOf(intArrayOf(Color.BLACK, Color.WHITE), intArrayOf(Color.BLACK, Color.RED),
                        intArrayOf(Color.BLACK, Color.GREEN), intArrayOf(Color.BLACK, Color.BLUE))
                    val bh = h / bands.size
                    for ((i, b) in bands.withIndex()) {
                        paint.shader = LinearGradient(0f, 0f, w, 0f, b[0], b[1], Shader.TileMode.CLAMP)
                        c.drawRect(0f, i * bh, w, (i + 1) * bh, paint)
                    }
                    paint.shader = null
                }
                mode == "grid" -> {
                    c.drawColor(Color.BLACK)
                    paint.color = Color.WHITE; paint.strokeWidth = 1f
                    var x = 0f; while (x <= w) { c.drawLine(x, 0f, x, h, paint); x += 40f }
                    var y = 0f; while (y <= h) { c.drawLine(0f, y, w, y, paint); y += 40f }
                }
                mode == "checker" -> {
                    paint.color = Color.WHITE
                    c.drawColor(Color.BLACK)
                    var y = 0
                    while (y < height) {
                        var x = if ((y / 2) % 2 == 0) 0 else 2
                        while (x < width) { c.drawRect(x.toFloat(), y.toFloat(), x + 2f, y + 2f, paint); x += 4 }
                        y += 2
                    }
                }
                mode == "touch" -> {
                    c.drawColor(Color.BLACK)
                    val cw = w / cols
                    val ch = h / rows
                    paint.style = Paint.Style.FILL
                    for (i in covered.indices) if (covered[i]) {
                        paint.color = Color.rgb(20, 70, 40)
                        c.drawRect((i % cols) * cw, (i / cols) * ch, (i % cols + 1) * cw, (i / cols + 1) * ch, paint)
                    }
                    paint.color = Color.DKGRAY; paint.strokeWidth = 1f
                    for (i in 1 until cols) c.drawLine(i * cw, 0f, i * cw, h, paint)
                    for (i in 1 until rows) c.drawLine(0f, i * ch, w, i * ch, paint)
                    paint.style = Paint.Style.STROKE; paint.strokeWidth = 6f
                    for ((p, col) in done) { paint.color = col; c.drawPath(p, paint) }
                    for ((id, p) in paths) { paint.color = pointerColors[id % pointerColors.size]; c.drawPath(p, paint) }
                    paint.style = Paint.Style.FILL
                    val pct = covered.count { it } * 100 / covered.size
                    c.drawText("touch: max $maxPointers fingers · coverage $pct %", 24f, 60f, text.apply { this.color = Color.WHITE })
                }
                mode == "fps" -> {
                    c.drawColor(Color.BLACK)
                    paint.color = Color.WHITE
                    val x = (phase / 360f) * w
                    c.drawRect(x - 40f, 0f, x + 40f, h, paint)
                }
            }
        }

        override fun onTouchEvent(e: MotionEvent): Boolean {
            if (mode == "sweep" && e.actionMasked == MotionEvent.ACTION_UP) {
                sweepIdx = (sweepIdx + 1) % SWEEP.size
                color = SWEEP[sweepIdx]
                Log.i(TAG, "{\"sweep\":$sweepIdx}")
                invalidate()
                return true
            }
            if (mode != "touch") return true
            val now = SystemClock.uptimeMillis()
            if (firstEvt == 0L) firstEvt = now
            events++
            maxPointers = maxOf(maxPointers, e.pointerCount)
            val cw = width.toFloat() / cols
            val ch = height.toFloat() / rows
            for (pi in 0 until e.pointerCount) {
                val id = e.getPointerId(pi)
                for (hI in 0 until e.historySize) mark(e.getHistoricalX(pi, hI), e.getHistoricalY(pi, hI), cw, ch)
                mark(e.getX(pi), e.getY(pi), cw, ch)
                when (e.actionMasked) {
                    MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN ->
                        if (pi == e.actionIndex) paths[id] = Path().apply { moveTo(e.getX(pi), e.getY(pi)) }
                    MotionEvent.ACTION_MOVE -> paths[id]?.lineTo(e.getX(pi), e.getY(pi))
                }
            }
            if (e.actionMasked == MotionEvent.ACTION_UP || e.actionMasked == MotionEvent.ACTION_POINTER_UP) {
                val id = e.getPointerId(e.actionIndex)
                paths.remove(id)?.let { done.add(it to pointerColors[id % pointerColors.size]) }
                if (done.size > 60) done.removeAt(0)
            }
            if (now - lastReport > 300 || e.actionMasked == MotionEvent.ACTION_UP) {
                lastReport = now
                val secs = (now - firstEvt) / 1000.0
                val rate = if (secs > 0.2) events / secs else 0.0
                val bits = covered.joinToString("") { if (it) "1" else "0" }
                Log.i(TAG, "{\"touch\":1,\"pointers\":${e.pointerCount},\"max\":$maxPointers,\"events\":$events," +
                    "\"rate\":%.0f,\"cols\":$cols,\"rows\":$rows,\"cov\":\"$bits\"}".format(rate))
            }
            invalidate()
            return true
        }

        private fun mark(x: Float, y: Float, cw: Float, ch: Float) {
            val cx = (x / cw).toInt().coerceIn(0, cols - 1)
            val cy = (y / ch).toInt().coerceIn(0, rows - 1)
            covered[cy * cols + cx] = true
        }
    }
}
