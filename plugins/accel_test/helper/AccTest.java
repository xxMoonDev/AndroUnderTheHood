// Accelerometer test helper for the AndroUnderTheHood "Accelerometer test" plugin.
// Runs on the device with:  CLASSPATH=acctest.jar app_process / AccTest list|stream ...
//
//   list                                  -> JSON: every sensor (the plugin picks the acceleration ones)
//   stream <index> <periodUs> [token]     -> text lines on stdout:
//        I {json}                 sensor info + start
//        D <t_ns> <v0> <v1> ...   one event (all values)
//        A <accuracy>             accuracy changed
//        K                        keep-alive (twice a second)
//        E <message>              error
// <index> is the position in SensorManager.getSensorList(TYPE_ALL) plus dynamic sensors, as listed by "list".
//
// Built against android.jar with javac --release 11 and d8 (see build_helper.sh).

import android.annotation.SuppressLint;
import android.app.Application;
import android.app.Instrumentation;
import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.pm.ApplicationInfo;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedOutputStream;
import java.io.FileDescriptor;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

@SuppressLint({"PrivateApi", "DiscouragedPrivateApi", "SoonBlockedPrivateApi", "NewApi"})
public final class AccTest {
    static final String PKG = "com.android.shell";
    static Context ctx;
    static PrintStream out;

    // ------------------------------------------------------------------ fake context (as in scrcpy)
    static final class FakeContext extends ContextWrapper {
        FakeContext(Context base) { super(base); }
        @Override public String getPackageName() { return PKG; }
        @Override public String getOpPackageName() { return PKG; }
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

    static SensorManager sensors(Looper looper) throws Exception {
        try {   // our own instance, bound to the fake context and our looper
            Class<?> k = Class.forName("android.hardware.SystemSensorManager");
            Constructor<?> c = k.getDeclaredConstructor(Context.class, Looper.class);
            c.setAccessible(true);
            return (SensorManager) c.newInstance(ctx, looper);
        } catch (Throwable t) {
            return (SensorManager) ctx.getSystemService(Context.SENSOR_SERVICE);
        }
    }

    static List<Sensor> all(SensorManager sm) {
        List<Sensor> l = new ArrayList<>(sm.getSensorList(Sensor.TYPE_ALL));
        try {
            for (Sensor s : sm.getDynamicSensorList(Sensor.TYPE_ALL)) if (!l.contains(s)) l.add(s);
        } catch (Throwable ignored) { }
        return l;
    }

    public static void main(String[] args) {
        out = new PrintStream(new BufferedOutputStream(new FileOutputStream(FileDescriptor.out), 1 << 14), false);
        try {
            if (Looper.getMainLooper() == null) Looper.prepareMainLooper();
        } catch (Throwable ignored) { }
        try {
            ctx = makeContext();
            if (args.length >= 1 && args[0].equals("list")) {
                out.println(list().toString());
                out.flush();
                System.exit(0);
            } else if (args.length >= 3 && args[0].equals("stream")) {
                stream(Integer.parseInt(args[1]), Integer.parseInt(args[2]));
            } else {
                System.err.println("usage: AccTest list | stream <index> <periodUs>");
                System.exit(2);
            }
        } catch (Throwable t) {
            out.println("E " + t.getClass().getSimpleName() + ": " + t.getMessage());
            out.flush();
            t.printStackTrace();
            System.exit(1);
        }
    }

    // ------------------------------------------------------------------ list
    static String mode(int m) {
        switch (m) {
            case 0: return "continuous";
            case 1: return "on-change";
            case 2: return "one-shot";
            case 3: return "special";
            default: return String.valueOf(m);
        }
    }

    static JSONObject list() throws Exception {
        HandlerThread ht = new HandlerThread("sensors");
        ht.start();
        SensorManager sm = sensors(ht.getLooper());
        JSONObject root = new JSONObject();
        root.put("sdk", Build.VERSION.SDK_INT);
        root.put("model", Build.MANUFACTURER + " " + Build.MODEL);
        root.put("uid", Process.myUid());
        JSONArray arr = new JSONArray();
        List<Sensor> l = all(sm);
        List<Sensor> dyn = new ArrayList<>();
        try { dyn = sm.getDynamicSensorList(Sensor.TYPE_ALL); } catch (Throwable ignored) { }
        for (int i = 0; i < l.size(); i++) {
            Sensor s = l.get(i);
            JSONObject o = new JSONObject();
            o.put("index", i);
            o.put("name", s.getName());
            o.put("vendor", s.getVendor());
            o.put("version", s.getVersion());
            o.put("type", s.getType());
            o.put("string_type", s.getStringType());
            o.put("power_ma", s.getPower());
            o.put("resolution", s.getResolution());
            o.put("max_range", s.getMaximumRange());
            o.put("min_delay_us", s.getMinDelay());
            o.put("max_delay_us", s.getMaxDelay());
            o.put("reporting", mode(s.getReportingMode()));
            o.put("wakeup", s.isWakeUpSensor());
            o.put("fifo_reserved", s.getFifoReservedEventCount());
            o.put("fifo_max", s.getFifoMaxEventCount());
            o.put("dynamic", dyn.contains(s));
            try { o.put("id", s.getId()); } catch (Throwable ignored) { }
            try { if (Build.VERSION.SDK_INT >= 33) o.put("highest_direct_rate", s.getHighestDirectReportRateLevel()); } catch (Throwable ignored) { }
            arr.put(o);
        }
        root.put("sensors", arr);
        return root;
    }

    // ------------------------------------------------------------------ stream
    static int parentPid() {
        try {
            byte[] b = new byte[512];
            int n;
            try (FileInputStream in = new FileInputStream("/proc/self/stat")) { n = in.read(b); }
            String s = new String(b, 0, Math.max(n, 0));
            return Integer.parseInt(s.substring(s.lastIndexOf(')') + 2).split(" ")[1]);
        } catch (Throwable t) {
            return -1;
        }
    }

    static final StringBuilder sb = new StringBuilder(256);

    static synchronized void emit(String line) {
        out.print(line);
        out.print('\n');
    }

    static synchronized void flush() {
        out.flush();
        if (out.checkError()) System.exit(0);       // PC closed the stream
    }

    static void stream(int index, int periodUs) throws Exception {
        HandlerThread ht = new HandlerThread("sensors");
        ht.start();
        Handler h = new Handler(ht.getLooper());
        final SensorManager sm = sensors(ht.getLooper());
        List<Sensor> l = all(sm);
        if (index < 0 || index >= l.size()) { emit("E sensor " + index + " is gone"); flush(); System.exit(1); }
        final Sensor s = l.get(index);
        final SensorEventListener listener = new SensorEventListener() {
            @Override public void onSensorChanged(SensorEvent e) {
                StringBuilder b = sb;
                b.setLength(0);
                b.append("D ").append(e.timestamp);
                for (float v : e.values) b.append(' ').append(v);
                emit(b.toString());
            }
            @Override public void onAccuracyChanged(Sensor sensor, int accuracy) {
                emit("A " + accuracy);
            }
        };
        boolean ok = sm.registerListener(listener, s, periodUs, 0, h);
        if (!ok) { emit("E the sensor service refused the listener"); flush(); System.exit(1); }
        Runtime.getRuntime().addShutdownHook(new Thread(() -> { try { sm.unregisterListener(listener); } catch (Throwable ignored) { } }));
        JSONObject info = new JSONObject();
        info.put("name", s.getName());
        info.put("type", s.getType());
        info.put("period_us", periodUs);
        info.put("boot_ns", SystemClock.elapsedRealtimeNanos());
        emit("I " + info);
        flush();
        int ppid = parentPid();
        long nextK = 0;
        while (true) {          // flush ~30 times a second, keep-alive twice a second, watch the parent
            SystemClock.sleep(33);
            long now = SystemClock.uptimeMillis();
            if (now >= nextK) {
                nextK = now + 500;
                emit("K");
                if (ppid > 1 && parentPid() != ppid) System.exit(0);
            }
            flush();
        }
    }
}
