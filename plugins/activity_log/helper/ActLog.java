// App-use & tap logger helper for the AndroUnderTheHood "Activity log" plugin.
// Runs on the device with:  CLASSPATH=actlog.jar app_process / ActLog <command> ...
//
//   watch [keys] <token>          -> live lines on stdout (see below); exits when the PC goes away
//   record <file> [keys] <token>  -> the same lines appended to <file>; keeps running without the PC
//   probe                          -> JSON: what works (UI automation, touch device, display)
//
// Lines (one JSON object after a one-letter tag):
//   I {...}  info: touch device, display size, what is available
//   C {...}  context changed: app (package + label), activity, screen title, browser URL
//   T {...}  touch gesture: tap / long press / swipe / pinch, position, the element under the finger
//   B {...}  hardware button (power, volume, ...)
//   S {...}  screen on / off
//   W msg    warning;  E msg  fatal error;  K  keep-alive (live mode only)
//
// Text typed on the keyboard is never logged. Keyboard key labels are logged only with the "keys" option, and
// never while a password field has focus.
//
// Built against android.jar with javac --release 11 and d8 (see build_helper.sh).

import android.accessibilityservice.AccessibilityServiceInfo;
import android.annotation.SuppressLint;
import android.app.Application;
import android.app.Instrumentation;
import android.app.UiAutomation;
import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Point;
import android.graphics.Rect;
import android.hardware.display.DisplayManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONObject;

import java.io.BufferedOutputStream;
import java.io.BufferedReader;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.PrintStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

@SuppressLint({"PrivateApi", "DiscouragedPrivateApi", "SoonBlockedPrivateApi", "NewApi", "WrongConstant"})
public final class ActLog {
    static String PKG = "com.android.shell";
    static Context ctx;
    static PrintStream out;
    static boolean live = true;
    static boolean withKeys = false;
    static UiAutomation ua;
    static Handler main;

    // ------------------------------------------------------------------ fake context (as in scrcpy)
    static final class FakeContext extends ContextWrapper {
        FakeContext(Context base) { super(base); }
        @Override public String getPackageName() { return PKG; }
        @Override public String getOpPackageName() { return PKG; }
        @Override public String getAttributionTag() { return null; }
        @Override public Context getApplicationContext() { return this; }
        @Override public AttributionSource getAttributionSource() {
            AttributionSource.Builder b = new AttributionSource.Builder(Process.myUid());
            b.setPackageName(PKG);
            return b.build();
        }
        @SuppressWarnings("unused") public int getDeviceId() { return 0; }
    }

    static Context makeContext() throws Exception {
        Class<?> at = Class.forName("android.app.ActivityThread");
        Constructor<?> c = at.getDeclaredConstructor();
        c.setAccessible(true);
        Object thread = c.newInstance();
        setField(at, null, "sCurrentActivityThread", thread);
        try { setField(at, thread, "mSystemThread", true); } catch (Throwable ignored) { }
        if (Build.VERSION.SDK_INT >= 31) {
            try {
                Class<?> cc = Class.forName("android.app.ConfigurationController");
                Class<?> ati = Class.forName("android.app.ActivityThreadInternal");
                Constructor<?> k = cc.getDeclaredConstructor(ati);
                k.setAccessible(true);
                setField(at, thread, "mConfigurationController", k.newInstance(thread));
            } catch (Throwable ignored) { }
        }
        try {
            Class<?> abd = Class.forName("android.app.ActivityThread$AppBindData");
            Constructor<?> k = abd.getDeclaredConstructor();
            k.setAccessible(true);
            Object data = k.newInstance();
            ApplicationInfo ai = new ApplicationInfo();
            ai.packageName = PKG;
            setField(abd, data, "appInfo", ai);
            setField(at, thread, "mBoundApplication", data);
        } catch (Throwable ignored) { }
        Method m = at.getDeclaredMethod("getSystemContext");
        m.setAccessible(true);
        FakeContext fc = new FakeContext((Context) m.invoke(thread));
        try {
            Application app = Instrumentation.newApplication(Application.class, fc);
            setField(at, thread, "mInitialApplication", app);
        } catch (Throwable ignored) { }
        return fc;
    }

    static void setField(Class<?> cls, Object obj, String name, Object value) throws Exception {
        Field f = cls.getDeclaredField(name);
        f.setAccessible(true);
        f.set(obj, value);
    }

    // ------------------------------------------------------------------ main
    public static void main(String[] args) {
        out = new PrintStream(new BufferedOutputStream(new FileOutputStream(FileDescriptor.out), 1 << 15), false);
        try {
            if (Looper.getMainLooper() == null) Looper.prepareMainLooper();
        } catch (Throwable ignored) { }
        try {
            if (Process.myUid() == 1000) PKG = "android";
            ctx = makeContext();
            main = new Handler(Looper.getMainLooper());
            String cmd = args.length > 0 ? args[0] : "";
            List<String> rest = new ArrayList<>();
            for (int i = 1; i < args.length; i++) rest.add(args[i]);
            withKeys = rest.remove("keys");
            switch (cmd) {
                case "probe": probe(); out.flush(); System.exit(0); break;
                case "watch": live = true; break;
                case "record": {
                    live = false;
                    OutputStream f = new FileOutputStream(rest.get(0), true);
                    out = new PrintStream(new BufferedOutputStream(f, 1 << 14), false);
                    break;
                }
                default:
                    System.err.println("usage: ActLog probe | watch [keys] <token> | record <file> [keys] <token>");
                    System.exit(2);
            }
            run();
        } catch (Throwable t) {
            emitRaw("E " + t.getClass().getSimpleName() + ": " + t.getMessage());
            t.printStackTrace();
            System.exit(1);
        }
    }

    static synchronized void emitRaw(String s) {
        out.print(s);
        out.print('\n');
        out.flush();
        if (out.checkError()) System.exit(0);       // PC closed the stream / disk full
    }

    static void emit(char tag, JSONObject o) {
        try { o.put("t", System.currentTimeMillis()); } catch (Exception ignored) { }
        emitRaw(tag + " " + o);
    }

    static void warn(String s) { emitRaw("W " + s.replace('\n', ' ')); }

    static JSONObject put(JSONObject o, String k, Object v) {
        try { if (v != null) o.put(k, v); } catch (Exception ignored) { }
        return o;
    }

    // ------------------------------------------------------------------ UI automation
    static UiAutomation connectUi() throws Exception {
        Class<?> conCls = Class.forName("android.app.UiAutomationConnection");
        Constructor<?> ck = conCls.getDeclaredConstructor();
        ck.setAccessible(true);
        Object con = ck.newInstance();
        Class<?> iface = Class.forName("android.app.IUiAutomationConnection");
        UiAutomation u;
        try {
            Constructor<UiAutomation> k = UiAutomation.class.getDeclaredConstructor(Looper.class, iface);
            k.setAccessible(true);
            u = k.newInstance(Looper.getMainLooper(), con);
        } catch (NoSuchMethodException e) {
            Constructor<UiAutomation> k = UiAutomation.class.getDeclaredConstructor(Context.class, iface);
            k.setAccessible(true);
            u = k.newInstance(ctx, con);
        }
        Method m = UiAutomation.class.getDeclaredMethod("connect", int.class);
        m.setAccessible(true);
        try {
            m.invoke(u, 1 /* FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES: the user's own services keep running */);
        } catch (java.lang.reflect.InvocationTargetException ite) {
            Throwable c = ite.getCause() != null ? ite.getCause() : ite;
            String msg = String.valueOf(c.getMessage());
            if (msg.contains("already registered") || msg.contains("already connected"))
                throw new IllegalStateException("Another tool is using Android's UI automation right now "
                        + "(uiautomator, Appium, a test runner, or another Activity log). Stop it and try again.");
            throw new IllegalStateException("UI automation refused: " + c);
        }
        AccessibilityServiceInfo info = u.getServiceInfo();
        if (info != null) {
            info.flags |= AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                    | AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                    | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS;
            info.eventTypes = AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED | AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED
                    | AccessibilityEvent.TYPE_WINDOWS_CHANGED | AccessibilityEvent.TYPE_VIEW_CLICKED
                    | AccessibilityEvent.TYPE_VIEW_LONG_CLICKED;
            info.notificationTimeout = 0;
            u.setServiceInfo(info);
        }
        return u;
    }

    static void probe() {
        JSONObject o = new JSONObject();
        put(o, "uid", Process.myUid());
        put(o, "sdk", Build.VERSION.SDK_INT);
        put(o, "model", Build.MANUFACTURER + " " + Build.MODEL);
        try {
            UiAutomation u = connectUi();
            AccessibilityNodeInfo r = u.getRootInActiveWindow();
            put(o, "ui", true);
            put(o, "foreground", r == null ? null : String.valueOf(r.getPackageName()));
            try { u.getClass().getMethod("disconnect").invoke(u); } catch (Throwable ignored) { }
        } catch (Throwable t) {
            put(o, "ui", false);
            put(o, "ui_error", t.getMessage());
        }
        Touch td = Touch.find();
        put(o, "touch", td == null ? null : td.path + " " + td.name);
        put(o, "touch_error", td == null ? Touch.lastError : null);
        emitRaw(o.toString());
    }

    // ------------------------------------------------------------------ run
    static void run() throws Exception {
        try {
            ua = connectUi();
        } catch (Throwable t) {
            emitRaw("E " + t.getMessage());
            out.flush();
            System.exit(1);
        }
        Touch td = Touch.find();
        JSONObject info = new JSONObject();
        put(info, "uid", Process.myUid());
        put(info, "sdk", Build.VERSION.SDK_INT);
        put(info, "model", Build.MANUFACTURER + " " + Build.MODEL);
        put(info, "keys", withKeys);
        Point sz = displaySize();
        put(info, "w", sz.x);
        put(info, "h", sz.y);
        put(info, "density", ctx.getResources().getDisplayMetrics().densityDpi);
        if (td != null) {
            put(info, "touch", td.path + " " + td.name);
            put(info, "touch_max", td.maxX + "x" + td.maxY);
        } else {
            put(info, "touch_error", Touch.lastError);
        }
        emit('I', info);
        if (td == null) warn("Raw touches can't be read (" + Touch.lastError + "); taps are taken from "
                + "Android's click events instead, so swipes and taps on plain areas are not logged.");
        ua.setOnAccessibilityEventListener(ActLog::onEvent);
        if (td != null) new Thread(td::loop, "touch").start();
        final int ppid = parentPid();
        new Thread(() -> {        // keep-alive, screen state, PC watchdog
            int state = -1;
            for (int tick = 0; ; tick++) {
                int s = screenOn() ? 1 : 0;
                if (s != state) {
                    state = s;
                    emit('S', put(new JSONObject(), "on", s == 1));
                    if (s == 1) main.post(() -> refresh(true));
                }
                if (live && tick % 4 == 0) {
                    emitRaw("K");
                    if (ppid > 1 && parentPid() != ppid) System.exit(0);
                }
                SystemClock.sleep(500);
            }
        }, "alive").start();
        main.post(() -> refresh(true));
        Looper.loop();
    }

    static int parentPid() {
        try (BufferedReader r = new BufferedReader(new java.io.FileReader("/proc/self/stat"))) {
            String s = r.readLine();
            String[] p = s.substring(s.lastIndexOf(')') + 2).split(" ");
            return Integer.parseInt(p[1]);
        } catch (Throwable t) {
            return -1;
        }
    }

    static Display display() {
        DisplayManager dm = (DisplayManager) ctx.getSystemService(Context.DISPLAY_SERVICE);
        return dm.getDisplay(Display.DEFAULT_DISPLAY);
    }

    static Point displaySize() {
        Point p = new Point();
        try { display().getRealSize(p); } catch (Throwable ignored) { }
        return p;
    }

    static boolean screenOn() {
        try { return display().getState() != Display.STATE_OFF; } catch (Throwable t) { return true; }
    }

    // ------------------------------------------------------------------ context (app / screen / URL)
    static final Map<String, String> labels = new HashMap<>();
    static final Map<String, String> activities = new HashMap<>();
    static String lastSig = "";
    static boolean refreshQueued = false;
    static long lastRefresh = 0;
    static String lastEventText = "";

    static void onEvent(AccessibilityEvent ev) {
        int type = ev.getEventType();
        CharSequence pkg = ev.getPackageName();
        if (type == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            CharSequence cls = ev.getClassName();
            if (pkg != null && cls != null) {
                String c = cls.toString();
                if (!c.startsWith("android.widget.") && !c.startsWith("android.view.") && !c.startsWith("android.app.Dialog")
                        && !c.startsWith("androidx.") && c.contains(".")) activities.put(pkg.toString(), c);
            }
            List<CharSequence> t = ev.getText();
            if (t != null && !t.isEmpty() && t.get(0) != null) lastEventText = t.get(0).toString();
            queueRefresh(150);
        } else if (type == AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED || type == AccessibilityEvent.TYPE_WINDOWS_CHANGED) {
            queueRefresh(700);
        } else if ((type == AccessibilityEvent.TYPE_VIEW_CLICKED || type == AccessibilityEvent.TYPE_VIEW_LONG_CLICKED)
                && !Touch.working) {
            AccessibilityNodeInfo n = ev.getSource();
            JSONObject o = new JSONObject();
            put(o, "g", type == AccessibilityEvent.TYPE_VIEW_CLICKED ? "click" : "long click");
            if (n != null) {
                Rect b = new Rect();
                n.getBoundsInScreen(b);
                put(o, "x", b.centerX());
                put(o, "y", b.centerY());
                describe(o, n, isImeWindow(n));
            } else {
                List<CharSequence> t = ev.getText();
                put(o, "label", t == null || t.isEmpty() ? null : clip(String.valueOf(t.get(0))));
            }
            put(o, "pkg", pkg == null ? null : pkg.toString());
            emit('T', o);
        }
    }

    static void queueRefresh(long delay) {
        if (refreshQueued) return;
        refreshQueued = true;
        long wait = Math.max(delay, 300 - (SystemClock.uptimeMillis() - lastRefresh));
        main.postDelayed(() -> { refreshQueued = false; refresh(false); }, wait);
    }

    static void refresh(boolean force) {
        lastRefresh = SystemClock.uptimeMillis();
        try {
            AccessibilityWindowInfo win = activeAppWindow();
            AccessibilityNodeInfo root = win != null ? win.getRoot() : ua.getRootInActiveWindow();
            if (root == null) return;
            String pkg = String.valueOf(root.getPackageName());
            String title = screenTitle(root, win);
            String url = browserUrl(root, pkg);
            String sig = pkg + "\u0001" + title + "\u0001" + url;
            if (!force && sig.equals(lastSig)) return;
            lastSig = sig;
            JSONObject o = new JSONObject();
            put(o, "pkg", pkg);
            put(o, "app", appLabel(pkg));
            put(o, "act", activities.get(pkg));
            title = title.endsWith(".") ? title.substring(0, title.length() - 1) : title;
            put(o, "title", title.isEmpty() ? null : title);
            put(o, "url", url.isEmpty() ? null : url);
            if (win != null && win.getType() != AccessibilityWindowInfo.TYPE_APPLICATION) put(o, "wtype", winType(win.getType()));
            emit('C', o);
        } catch (Throwable t) {
            warn("context: " + t);
        }
    }

    /** The window the user is looking at: the focused one, unless it is the keyboard or a small overlay. */
    static AccessibilityWindowInfo activeAppWindow() {
        List<AccessibilityWindowInfo> ws;
        try { ws = ua.getWindows(); } catch (Throwable t) { return null; }
        AccessibilityWindowInfo best = null;
        for (AccessibilityWindowInfo w : ws) {
            if (w.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD
                    || w.getType() == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY
                    || w.getType() == AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER) continue;
            if (w.isActive()) return w;
            if (best == null && w.getType() == AccessibilityWindowInfo.TYPE_APPLICATION) best = w;
        }
        return best;
    }

    static String winType(int t) {
        switch (t) {
            case AccessibilityWindowInfo.TYPE_APPLICATION: return "app";
            case AccessibilityWindowInfo.TYPE_INPUT_METHOD: return "keyboard";
            case AccessibilityWindowInfo.TYPE_SYSTEM: return "system";
            case AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY: return "overlay";
            case AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER: return "divider";
            case 6: return "magnification";
            default: return "type" + t;
        }
    }

    static String appLabel(String pkg) {
        String l = labels.get(pkg);
        if (l != null) return l;
        try {
            PackageManager pm = ctx.getPackageManager();
            l = String.valueOf(pm.getApplicationInfo(pkg, 0).loadLabel(pm));
        } catch (Throwable t) {
            l = pkg;
        }
        labels.put(pkg, l);
        return l;
    }

    static final Pattern BAR_ID = Pattern.compile("(?i)(^|[:/_])(toolbar|action_?bar|app_?bar|collapsing|title_?bar|"
            + "header|top_?bar|search_?bar)");
    static final Pattern TITLE_ID = Pattern.compile("(?i)(title|toolbar|action_bar)");

    /** Page title: the toolbar / action bar heading, else the window title, else the last window event text. */
    static String screenTitle(AccessibilityNodeInfo root, AccessibilityWindowInfo win) {
        // system windows (lock screen, notification shade) name themselves better than their contents do
        if (win != null && win.getType() != AccessibilityWindowInfo.TYPE_APPLICATION && win.getTitle() != null
                && win.getTitle().length() > 0) return clip(win.getTitle().toString().replaceAll("\\.$", ""));
        ArrayDeque<AccessibilityNodeInfo> q = new ArrayDeque<>();
        q.add(root);
        int seen = 0;
        while (!q.isEmpty() && seen < 600) {
            AccessibilityNodeInfo n = q.poll();
            seen++;
            String id = n.getViewIdResourceName();
            String cls = String.valueOf(n.getClassName());
            if ((id != null && BAR_ID.matcher(id).find()) || cls.endsWith("Toolbar") || cls.endsWith("ActionBar")) {
                String t = firstText(n, 0);
                if (t != null) return t;
            }
            if (id != null && id.endsWith(":id/title") && n.getText() != null && n.getText().length() > 0
                    && seen < 40) return clip(n.getText().toString());
            if (n.isHeading() && n.getText() != null && n.getText().length() > 0) return clip(n.getText().toString());
            for (int i = 0; i < n.getChildCount(); i++) {
                AccessibilityNodeInfo c = n.getChild(i);
                if (c != null) q.add(c);
            }
        }
        if (win != null && win.getTitle() != null && win.getTitle().length() > 0) return clip(win.getTitle().toString());
        return clip(lastEventText);
    }

    /** First non-button text inside a bar (the heading), skipping navigation / menu buttons. */
    static String firstText(AccessibilityNodeInfo n, int depth) {
        if (n == null || depth > 6) return null;
        CharSequence t = n.getText();
        String cls = String.valueOf(n.getClassName());
        if (t != null && t.length() > 0 && !cls.contains("Button") && !cls.contains("EditText")) return clip(t.toString());
        if (depth == 0 && n.getContentDescription() != null && cls.contains("CollapsingToolbar"))
            return clip(n.getContentDescription().toString());
        for (int i = 0; i < n.getChildCount(); i++) {
            String r = firstText(n.getChild(i), depth + 1);
            if (r != null) return r;
        }
        return null;
    }

    static final String[] URL_IDS = {
            "com.android.chrome:id/url_bar", "com.chrome.beta:id/url_bar", "com.chrome.dev:id/url_bar",
            "com.chrome.canary:id/url_bar", "com.brave.browser:id/url_bar", "com.microsoft.emmx:id/url_bar",
            "com.vivaldi.browser:id/url_bar", "com.kiwibrowser.browser:id/url_bar", "org.bromite.bromite:id/url_bar",
            "com.sec.android.app.sbrowser:id/location_bar_edit_text", "com.opera.browser:id/url_field",
            "com.opera.mini.native:id/url_field", "org.mozilla.firefox:id/mozac_browser_toolbar_url_view",
            "org.mozilla.firefox_beta:id/mozac_browser_toolbar_url_view", "org.mozilla.focus:id/display_url",
            "com.duckduckgo.mobile.android:id/omnibarTextInput", "com.heytap.browser:id/url_bar",
            "com.android.browser:id/url", "com.mi.globalbrowser:id/url",
    };
    static final Pattern URL_GENERIC = Pattern.compile("(?i):id/(url|url_bar|url_field|location_bar\\w*|omnibox\\w*|"
            + "address_bar\\w*|search_box_text|mozac_browser_toolbar_url_view)$");

    static String browserUrl(AccessibilityNodeInfo root, String pkg) {
        for (String id : URL_IDS) {
            if (!id.startsWith(pkg + ":")) continue;
            List<AccessibilityNodeInfo> l = root.findAccessibilityNodeInfosByViewId(id);
            if (l != null) for (AccessibilityNodeInfo n : l) {
                if (n.getText() != null && n.getText().length() > 0 && !n.isFocused()) return clip(n.getText().toString(), 300);
            }
        }
        // any app: an address field, or the web page shown by a WebView
        ArrayDeque<AccessibilityNodeInfo> q = new ArrayDeque<>();
        q.add(root);
        int seen = 0;
        String web = "";
        while (!q.isEmpty() && seen < 500) {
            AccessibilityNodeInfo n = q.poll();
            seen++;
            String id = n.getViewIdResourceName();
            if (id != null && URL_GENERIC.matcher(id).find() && n.getText() != null && n.getText().length() > 0
                    && !n.isFocused() && looksLikeUrl(n.getText().toString())) return clip(n.getText().toString(), 300);
            if (web.isEmpty() && "android.webkit.WebView".contentEquals(n.getClassName() == null ? "" : n.getClassName())) {
                CharSequence t = n.getText();
                if (t == null || t.length() == 0) t = n.getContentDescription();
                if (t != null && looksLikeUrl(t.toString())) web = clip(t.toString(), 300);
            }
            for (int i = 0; i < n.getChildCount(); i++) {
                AccessibilityNodeInfo c = n.getChild(i);
                if (c != null) q.add(c);
            }
        }
        return web;
    }

    static boolean looksLikeUrl(String s) {
        s = s.trim();
        return !s.contains(" ") && (s.contains("://") || s.matches("(?i)^[\\w-]+(\\.[\\w-]+)+(:\\d+)?(/.*)?$"));
    }

    static String clip(String s) { return clip(s, 100); }

    static String clip(String s, int max) {
        if (s == null) return "";
        s = s.replaceAll("[\\s\\u200b]+", " ").trim();
        return s.length() > max ? s.substring(0, max - 1) + "…" : s;
    }

    // ------------------------------------------------------------------ what is under a point
    static boolean isImeWindow(AccessibilityNodeInfo n) {
        try {
            AccessibilityWindowInfo w = n.getWindow();
            return w != null && w.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD;
        } catch (Throwable t) {
            return false;
        }
    }

    /** Fills label/kind/id/state for the element that would receive a tap at (x, y). */
    static void lookup(JSONObject o, int x, int y) {
        try {
            List<AccessibilityWindowInfo> ws = new ArrayList<>(ua.getWindows());
            ws.sort((a, b) -> Integer.compare(b.getLayer(), a.getLayer()));
            Rect r = new Rect();
            for (AccessibilityWindowInfo w : ws) {
                w.getBoundsInScreen(r);
                if (!r.contains(x, y)) continue;
                if (w.getType() == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY) continue;
                AccessibilityNodeInfo root = w.getRoot();
                boolean ime = w.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD;
                put(o, "win", winType(w.getType()));
                if (root != null) put(o, "pkg", String.valueOf(root.getPackageName()));
                if (root == null) {
                    put(o, "label", ime ? "Keyboard" : (w.getTitle() == null ? null : clip(w.getTitle().toString())));
                    return;
                }
                AccessibilityNodeInfo hit = deepest(root, x, y, 0);
                if (hit == null) hit = root;
                describe(o, hit, ime);
                if (o.optString("label", "").isEmpty() && w.getTitle() != null) put(o, "label", clip(w.getTitle().toString()));
                return;
            }
        } catch (Throwable t) {
            put(o, "err", String.valueOf(t));
        }
    }

    static AccessibilityNodeInfo deepest(AccessibilityNodeInfo n, int x, int y, int depth) {
        if (n == null || depth > 60) return null;
        Rect r = new Rect();
        n.getBoundsInScreen(r);
        if (!r.contains(x, y) || !n.isVisibleToUser()) return null;
        for (int i = n.getChildCount() - 1; i >= 0; i--) {     // later children are drawn on top
            AccessibilityNodeInfo d = deepest(n.getChild(i), x, y, depth + 1);
            if (d != null) return d;
        }
        return n;
    }

    static void describe(JSONObject o, AccessibilityNodeInfo hit, boolean ime) {
        AccessibilityNodeInfo target = hit;
        for (AccessibilityNodeInfo p = hit; p != null; p = p.getParent()) {
            if (p.isClickable() || p.isLongClickable() || p.isCheckable() || p.isEditable()) { target = p; break; }
        }
        String cls = String.valueOf(target.getClassName());
        put(o, "kind", cls.substring(cls.lastIndexOf('.') + 1));
        String id = target.getViewIdResourceName();
        if (id != null) put(o, "id", id.substring(id.indexOf('/') + 1));
        if (ime) {
            put(o, "kind", "keyboard");
            String key = label(target);
            put(o, "label", withKeys && !passwordFocused() && !key.isEmpty() ? "Keyboard: " + key : "Keyboard");
            return;
        }
        if (target.isPassword()) {
            put(o, "label", "password field");
            return;
        }
        if (target.isEditable()) {
            CharSequence h = target.getHintText();
            put(o, "label", "text field" + (h != null && h.length() > 0 ? " '" + clip(h.toString(), 60) + "'" : ""));
            return;
        }
        put(o, "label", label(target));
        if (target.isCheckable()) put(o, "was", target.isChecked() ? "on" : "off");
        if (!target.isEnabled()) put(o, "disabled", true);
    }

    /** Readable name: own text / description, else the texts inside it (a list row's title and summary). */
    static String label(AccessibilityNodeInfo n) {
        CharSequence t = n.getText();
        if (t != null && t.length() > 0 && !n.isPassword()) return clip(t.toString());
        t = n.getContentDescription();
        if (t != null && t.length() > 0) return clip(t.toString());
        StringBuilder sb = new StringBuilder();
        collect(n, sb, 0);
        if (sb.length() > 0) return clip(sb.toString());
        t = n.getHintText();
        if (t != null && t.length() > 0) return clip(t.toString());
        String id = n.getViewIdResourceName();
        return id == null ? "" : id.substring(id.indexOf('/') + 1).replace('_', ' ');
    }

    static void collect(AccessibilityNodeInfo n, StringBuilder sb, int depth) {
        if (depth > 8 || sb.length() > 100) return;
        for (int i = 0; i < n.getChildCount(); i++) {
            AccessibilityNodeInfo c = n.getChild(i);
            if (c == null) continue;
            CharSequence t = c.isPassword() ? null : c.getText();
            if (t == null || t.length() == 0) t = c.getContentDescription();
            if (t != null && t.length() > 0) {
                if (sb.length() > 0) sb.append(" · ");
                sb.append(t);
            } else {
                collect(c, sb, depth + 1);
            }
        }
    }

    static boolean passwordFocused() {
        try {
            for (AccessibilityWindowInfo w : ua.getWindows()) {
                if (w.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD) continue;
                AccessibilityNodeInfo r = w.getRoot();
                AccessibilityNodeInfo f = r == null ? null : r.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
                if (f != null && f.isPassword()) return true;
            }
        } catch (Throwable ignored) { }
        return false;
    }

    // ------------------------------------------------------------------ raw touches (getevent)
    static final class Touch {
        static String lastError = "";
        static volatile boolean working = false;
        String path, name;
        int maxX, maxY;

        static Touch find() {
            try {
                java.lang.Process p = new ProcessBuilder("getevent", "-lp").redirectErrorStream(true).start();
                BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()));
                String line, dev = null, name = "";
                int mx = -1;
                StringBuilder all = new StringBuilder();
                while ((line = r.readLine()) != null) {
                    if (all.length() < 300) all.append(line).append(' ');
                    line = line.trim();
                    if (line.startsWith("add device")) {
                        dev = line.substring(line.indexOf('/'));
                        name = "";
                        mx = -1;
                    } else if (line.startsWith("name:")) {
                        name = line.substring(5).trim().replace("\"", "");
                    } else if (line.startsWith("ABS_MT_POSITION_X")) {
                        mx = maxOf(line);
                    } else if (line.startsWith("ABS_MT_POSITION_Y") && dev != null && mx > 0) {
                        Touch t = new Touch();
                        t.path = dev;
                        t.name = name;
                        t.maxX = mx;
                        t.maxY = maxOf(line);
                        p.destroy();
                        return t;
                    }
                }
                p.waitFor();
                lastError = all.toString().contains("ermission") ? "no permission for /dev/input"
                        : "no multi-touch screen found";
            } catch (Throwable t) {
                lastError = String.valueOf(t);
            }
            return null;
        }

        static int maxOf(String line) {
            java.util.regex.Matcher m = Pattern.compile("max (\\d+)").matcher(line);
            return m.find() ? Integer.parseInt(m.group(1)) : -1;
        }

        // per-slot state
        final Map<Integer, int[]> slots = new HashMap<>();       // slot -> {curX, curY, startX, startY}
        int slot = 0;
        boolean anyTracking = false, btnDown = false;
        boolean down = false;
        long t0 = 0, tLast = 0;
        int fingers = 0;
        JSONObject pending;
        int[] first;
        float[] firstStart, firstEnd, secondStart, secondEnd;

        void loop() {
            try {
                java.lang.Process p = new ProcessBuilder("getevent", "-lt").redirectErrorStream(true).start();
                BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()), 1 << 16);
                String line;
                Map<String, String> devNames = new HashMap<>();
                working = true;
                while ((line = r.readLine()) != null) {
                    if (line.startsWith("add device")) {
                        String path = line.substring(line.indexOf('/')).trim();
                        String nm = r.readLine();
                        devNames.put(path, nm == null ? "" : nm.replaceAll(".*name:\\s*", "").replace("\"", "").trim());
                        continue;
                    }
                    int rb = line.indexOf(']');
                    if (rb < 0) continue;
                    double ts;
                    try { ts = Double.parseDouble(line.substring(1, rb).trim()); } catch (Exception e) { continue; }
                    String rest = line.substring(rb + 1).trim();
                    int colon = rest.indexOf(": ");
                    if (colon < 0) continue;
                    String dev = rest.substring(0, colon);
                    String[] f = rest.substring(colon + 2).trim().split("\\s+");
                    if (f.length < 3) continue;
                    if (dev.equals(path)) touchEvent(f[0], f[1], f[2], (long) (ts * 1000));
                    else if (f[0].equals("EV_KEY") && f[2].equals("DOWN")) keyEvent(f[1], devNames.get(dev));
                }
                warn("getevent stopped");
            } catch (Throwable t) {
                warn("touch reader: " + t);
            }
            working = false;
        }

        void keyEvent(String code, String dev) {
            if (code.startsWith("BTN_")) return;
            String k = code.startsWith("KEY_") ? code.substring(4) : code;
            String nice;
            switch (k) {
                case "POWER": nice = "Power button"; break;
                case "VOLUMEUP": nice = "Volume up"; break;
                case "VOLUMEDOWN": nice = "Volume down"; break;
                case "BACK": nice = "Back key"; break;
                case "HOMEPAGE": case "HOME": nice = "Home key"; break;
                case "APPSELECT": nice = "Recents key"; break;
                case "WAKEUP": nice = "Wake-up gesture"; break;
                default: nice = k.charAt(0) + k.substring(1).toLowerCase().replace('_', ' ');
            }
            JSONObject o = new JSONObject();
            put(o, "key", nice);
            put(o, "dev", dev);
            emit('B', o);
        }

        void touchEvent(String type, String code, String val, long ms) {
            switch (code) {
                case "ABS_MT_SLOT": slot = hex(val); break;
                case "ABS_MT_TRACKING_ID":
                    anyTracking = true;
                    if (val.equalsIgnoreCase("ffffffff")) slots.remove(slot);
                    else slots.put(slot, new int[]{-1, -1, -1, -1});
                    break;
                case "ABS_MT_POSITION_X": case "ABS_X": {
                    int[] s = cur();
                    if (s != null) s[0] = hex(val);
                    break;
                }
                case "ABS_MT_POSITION_Y": case "ABS_Y": {
                    int[] s = cur();
                    if (s != null) s[1] = hex(val);
                    break;
                }
                case "BTN_TOUCH":
                    btnDown = val.equals("DOWN");
                    if (!anyTracking) {
                        if (btnDown) slots.put(0, new int[]{-1, -1, -1, -1});
                        else slots.clear();
                    }
                    break;
                case "SYN_REPORT": frame(ms); break;
                default: break;
            }
        }

        int[] cur() {
            int[] s = slots.get(slot);
            if (s == null && !anyTracking) {
                s = new int[]{-1, -1, -1, -1};
                slots.put(slot, s);
            }
            return s;
        }

        static int hex(String v) {
            try { return (int) Long.parseLong(v, 16); } catch (Exception e) { return 0; }
        }

        void frame(long ms) {
            int n = 0;
            for (int[] s : slots.values()) {
                if (s[0] < 0 || s[1] < 0) continue;
                if (s[2] < 0) { s[2] = s[0]; s[3] = s[1]; }
                n++;
            }
            if (!down && n > 0) {                     // gesture starts: note what is under the finger NOW
                down = true;
                t0 = ms;
                fingers = n;
                int[] s = firstSlot();
                firstStart = toScreen(s[2], s[3]);
                firstEnd = firstStart;
                secondStart = secondEnd = null;
                pending = new JSONObject();
                lookup(pending, Math.round(firstStart[0]), Math.round(firstStart[1]));
            } else if (down && n > 0) {
                fingers = Math.max(fingers, n);
                int[] s = firstSlot();
                firstEnd = toScreen(s[0], s[1]);
                if (n >= 2) {
                    int[] s2 = secondSlot();
                    if (s2 != null) {
                        if (secondStart == null) secondStart = toScreen(s2[2], s2[3]);
                        secondEnd = toScreen(s2[0], s2[1]);
                    }
                }
            } else if (down && n == 0) {              // all fingers lifted
                down = false;
                finish(ms - t0);
            }
            tLast = ms;
        }

        int firstKey = -1;

        int[] firstSlot() {
            int best = Integer.MAX_VALUE;
            int[] r = null;
            for (Map.Entry<Integer, int[]> e : slots.entrySet()) {
                if (e.getValue()[0] < 0) continue;
                if (e.getKey() < best) { best = e.getKey(); r = e.getValue(); }
            }
            return r;
        }

        int[] secondSlot() {
            int[] a = firstSlot();
            for (int[] v : slots.values()) if (v != a && v[0] >= 0) return v;
            return null;
        }

        float[] toScreen(int rx, int ry) {
            Display d = display();
            int rot = d.getRotation();
            Point real = new Point();
            d.getRealSize(real);
            int nw = (rot % 2 == 0) ? real.x : real.y;    // size in the natural (portrait) orientation
            int nh = (rot % 2 == 0) ? real.y : real.x;
            float nx = (rx + 0.5f) * nw / (maxX + 1);
            float ny = (ry + 0.5f) * nh / (maxY + 1);
            switch (rot) {
                case 1: return new float[]{ny, nw - nx};
                case 2: return new float[]{nw - nx, nh - ny};
                case 3: return new float[]{nh - ny, nx};
                default: return new float[]{nx, ny};
            }
        }

        void finish(long dur) {
            JSONObject o = pending == null ? new JSONObject() : pending;
            pending = null;
            float dpx = ctx.getResources().getDisplayMetrics().densityDpi / 160f;
            float dx = firstEnd[0] - firstStart[0], dy = firstEnd[1] - firstStart[1];
            float dist = (float) Math.hypot(dx, dy) / dpx;
            Point real = displaySize();
            String g;
            if (fingers >= 2 && secondStart != null && secondEnd != null) {
                double a = Math.hypot(secondStart[0] - firstStart[0], secondStart[1] - firstStart[1]);
                double b = Math.hypot(secondEnd[0] - firstEnd[0], secondEnd[1] - firstEnd[1]);
                if (b > a * 1.25) g = "pinch out (zoom in)";
                else if (b < a * 0.8) g = "pinch in (zoom out)";
                else g = fingers + "-finger " + (dist < 16 ? "tap" : "swipe " + dir(dx, dy));
            } else if (dist < 16) {
                g = dur >= 500 ? "long press" : "tap";
            } else {
                String d = dir(dx, dy);
                float edge = 24 * dpx;
                if ((d.equals("right") && firstStart[0] < edge) || (d.equals("left") && firstStart[0] > real.x - edge))
                    g = "back gesture";
                else if (d.equals("up") && firstStart[1] > real.y - 30 * dpx)
                    g = dur > 350 || dist > 250 ? "home / recents gesture" : "home gesture";
                else if (d.equals("down") && firstStart[1] < 40 * dpx)
                    g = "pull down (notifications)";
                else g = (dur < 250 ? "fling " : "swipe ") + d;
            }
            put(o, "g", g);
            put(o, "x", Math.round(firstStart[0]));
            put(o, "y", Math.round(firstStart[1]));
            if (dist >= 16) {
                put(o, "x2", Math.round(firstEnd[0]));
                put(o, "y2", Math.round(firstEnd[1]));
            }
            put(o, "ms", dur);
            if (fingers > 1) put(o, "fingers", fingers);
            emit('T', o);
            main.post(() -> queueRefresh(400));
        }

        static String dir(float dx, float dy) {
            if (Math.abs(dx) > Math.abs(dy)) return dx > 0 ? "right" : "left";
            return dy > 0 ? "down" : "up";
        }
    }
}
