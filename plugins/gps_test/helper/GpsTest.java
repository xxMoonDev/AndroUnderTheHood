// GPS / location test helper for the AndroUnderTheHood "GPS & location test" plugin.
// Runs on the device with:  CLASSPATH=gpstest.jar app_process / GpsTest list|track ...
//
//   list                                 -> JSON: providers, GNSS hardware (model, year, capabilities), last fixes
//   track <provider> <intervalMs> [token] -> text lines on stdout:
//        I {json}      started
//        L {json}      a location fix
//        S {json}      satellite status (at most once a second)
//        F <ttff ms>   first fix
//        P on|off      provider enabled/disabled
//        K             keep-alive (once a second)
//        E <message>   error
//
// Built against android.jar with javac --release 11 and d8 (see build_helper.sh).

import android.annotation.SuppressLint;
import android.app.Application;
import android.app.Instrumentation;
import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.pm.ApplicationInfo;
import android.location.GnssCapabilities;
import android.location.GnssStatus;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.location.provider.ProviderProperties;
import android.os.Build;
import android.os.Bundle;
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

@SuppressLint({"PrivateApi", "DiscouragedPrivateApi", "SoonBlockedPrivateApi", "NewApi", "MissingPermission"})
public final class GpsTest {
    static String PKG = "com.android.shell";      // "android" when running as the system user (uid 1000)
    static Context ctx;
    static PrintStream out;

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

    static LocationManager lmInstance;

    static LocationManager lm() {
        if (lmInstance != null) return lmInstance;
        LocationManager m = (LocationManager) ctx.getSystemService(Context.LOCATION_SERVICE);
        // The service object comes from the system context, so requests would carry package "android" and be
        // refused for our uid. Point it at the fake context (package com.android.shell), as scrcpy does for others.
        try {
            Field f = LocationManager.class.getDeclaredField("mContext");
            f.setAccessible(true);
            f.set(m, ctx);
        } catch (Throwable t) {
            System.err.println("could not rebind LocationManager context: " + t);
        }
        lmInstance = m;
        return m;
    }

    public static void main(String[] args) {
        out = new PrintStream(new BufferedOutputStream(new FileOutputStream(FileDescriptor.out), 1 << 14), false);
        try {
            if (Looper.getMainLooper() == null) Looper.prepareMainLooper();
        } catch (Throwable ignored) { }
        try {
            if (Process.myUid() == 1000) PKG = "android";
            ctx = makeContext();
            if (args.length >= 1 && args[0].equals("list")) {
                out.println(list().toString());
                out.flush();
                System.exit(0);
            } else if (args.length >= 3 && args[0].equals("track")) {
                track(args[1], Long.parseLong(args[2]));
            } else {
                System.err.println("usage: GpsTest list | track <provider> <intervalMs>");
                System.exit(2);
            }
        } catch (Throwable t) {
            out.println("E " + t.getClass().getSimpleName() + ": " + t.getMessage());
            out.flush();
            t.printStackTrace();
            System.exit(1);
        }
    }

    // ------------------------------------------------------------------ helpers
    static String constellation(int c) {
        switch (c) {
            case 1: return "GPS";
            case 2: return "SBAS";
            case 3: return "GLONASS";
            case 4: return "QZSS";
            case 5: return "BeiDou";
            case 6: return "Galileo";
            case 7: return "NavIC";
            default: return "Unknown";
        }
    }

    static JSONObject loc(Location l) throws Exception {
        JSONObject o = new JSONObject();
        o.put("provider", l.getProvider());
        o.put("lat", l.getLatitude());
        o.put("lon", l.getLongitude());
        o.put("time", l.getTime());
        o.put("age_ms", (SystemClock.elapsedRealtimeNanos() - l.getElapsedRealtimeNanos()) / 1000000);
        if (l.hasAltitude()) o.put("alt", l.getAltitude());
        if (l.hasAccuracy()) o.put("acc", l.getAccuracy());
        if (Build.VERSION.SDK_INT >= 26 && l.hasVerticalAccuracy()) o.put("vacc", l.getVerticalAccuracyMeters());
        if (l.hasSpeed()) o.put("speed", l.getSpeed());
        if (l.hasBearing()) o.put("bearing", l.getBearing());
        if (Build.VERSION.SDK_INT >= 34 && l.hasMslAltitude()) o.put("msl_alt", l.getMslAltitudeMeters());
        if (Build.VERSION.SDK_INT >= 31) o.put("mock", l.isMock());
        else if (Build.VERSION.SDK_INT >= 18) o.put("mock", l.isFromMockProvider());
        Bundle e = l.getExtras();
        if (e != null && e.containsKey("satellites")) o.put("sats_used", e.getInt("satellites"));
        return o;
    }

    static JSONObject capabilities(LocationManager m) {
        JSONObject o = new JSONObject();
        if (Build.VERSION.SDK_INT < 30) return o;
        try {
            GnssCapabilities c = m.getGnssCapabilities();
            for (Method k : GnssCapabilities.class.getMethods()) {
                if (k.getName().startsWith("has") && k.getParameterCount() == 0 && k.getReturnType() == boolean.class) {
                    try { o.put(k.getName().substring(3), (boolean) k.invoke(c)); } catch (Throwable ignored) { }
                }
            }
        } catch (Throwable t) {
            try { o.put("error", String.valueOf(t)); } catch (Throwable ignored) { }
        }
        return o;
    }

    // ------------------------------------------------------------------ list
    static JSONObject list() throws Exception {
        LocationManager m = lm();
        JSONObject root = new JSONObject();
        root.put("sdk", Build.VERSION.SDK_INT);
        root.put("model", Build.MANUFACTURER + " " + Build.MODEL);
        root.put("uid", Process.myUid());
        try { root.put("location_enabled", Build.VERSION.SDK_INT >= 28 ? m.isLocationEnabled() : m.isProviderEnabled("gps")); } catch (Throwable t) { root.put("location_enabled_error", String.valueOf(t)); }
        try { if (Build.VERSION.SDK_INT >= 28) root.put("gnss_model", m.getGnssHardwareModelName()); } catch (Throwable ignored) { }
        try { if (Build.VERSION.SDK_INT >= 28) root.put("gnss_year", m.getGnssYearOfHardware()); } catch (Throwable ignored) { }
        root.put("capabilities", capabilities(m));
        JSONArray provs = new JSONArray();
        for (String p : m.getAllProviders()) {
            JSONObject o = new JSONObject();
            o.put("name", p);
            try { o.put("enabled", m.isProviderEnabled(p)); } catch (Throwable ignored) { }
            if (Build.VERSION.SDK_INT >= 31) {
                try {
                    ProviderProperties pp = m.getProviderProperties(p);
                    if (pp != null) {
                        o.put("accuracy", pp.getAccuracy() == ProviderProperties.ACCURACY_FINE ? "fine" : "coarse");
                        o.put("power", pp.getPowerUsage() == ProviderProperties.POWER_USAGE_HIGH ? "high"
                                : pp.getPowerUsage() == ProviderProperties.POWER_USAGE_MEDIUM ? "medium" : "low");
                        o.put("network", pp.hasNetworkRequirement());
                        o.put("satellite", pp.hasSatelliteRequirement());
                        o.put("cell", pp.hasCellRequirement());
                        o.put("altitude", pp.hasAltitudeSupport());
                        o.put("speed", pp.hasSpeedSupport());
                        o.put("bearing", pp.hasBearingSupport());
                    }
                } catch (Throwable ignored) { }
            }
            try {
                Location l = m.getLastKnownLocation(p);
                if (l != null) o.put("last", loc(l));
            } catch (Throwable t) {
                o.put("last_error", String.valueOf(t.getMessage()));
            }
            provs.put(o);
        }
        root.put("providers", provs);
        return root;
    }

    // ------------------------------------------------------------------ track
    static synchronized void emit(String s) {
        out.print(s);
        out.print('\n');
        out.flush();
        if (out.checkError()) System.exit(0);       // PC closed the stream
    }

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

    static long lastStatus = 0;
    static Location latest;

    static void track(String provider, long intervalMs) throws Exception {
        final LocationManager m = lm();
        HandlerThread ht = new HandlerThread("gps");
        ht.start();
        Looper looper = ht.getLooper();
        Handler h = new Handler(looper);
        if (!m.getAllProviders().contains(provider)) { emit("E provider '" + provider + "' does not exist on this device"); System.exit(1); }

        final LocationListener listener = new LocationListener() {
            @Override public void onLocationChanged(Location l) {
                synchronized (GpsTest.class) { latest = l; }     // reported on the interval clock below
            }
            @Override public void onProviderEnabled(String p) { emit("P on"); }
            @Override public void onProviderDisabled(String p) { emit("P off"); }
            @Override public void onStatusChanged(String p, int s, Bundle b) { }
        };
        GnssStatus.Callback cb = null;
        if (Build.VERSION.SDK_INT >= 24) {
            cb = new GnssStatus.Callback() {
                @Override public void onFirstFix(int ttff) { emit("F " + ttff); }
                @Override public void onStarted() { emit("G started"); }
                @Override public void onStopped() { emit("G stopped"); }
                @Override public void onSatelliteStatusChanged(GnssStatus s) {
                    long now = SystemClock.uptimeMillis();
                    if (now - lastStatus < 1000) return;
                    lastStatus = now;
                    try {
                        JSONArray arr = new JSONArray();
                        for (int i = 0; i < s.getSatelliteCount(); i++) {
                            JSONArray a = new JSONArray();
                            a.put(constellation(s.getConstellationType(i)));
                            a.put(s.getSvid(i));
                            a.put(Math.round(s.getCn0DbHz(i) * 10) / 10.0);
                            a.put(s.usedInFix(i));
                            a.put(Math.round(s.getElevationDegrees(i)));
                            a.put(Math.round(s.getAzimuthDegrees(i)));
                            a.put(Build.VERSION.SDK_INT >= 26 && s.hasCarrierFrequencyHz(i) ? Math.round(s.getCarrierFrequencyHz(i) / 1e5) / 10.0 : 0);
                            a.put(s.hasAlmanacData(i));
                            a.put(s.hasEphemerisData(i));
                            arr.put(a);
                        }
                        JSONObject o = new JSONObject();
                        o.put("sats", arr);
                        emit("S " + o);
                    } catch (Throwable t) { emit("E " + t); }
                }
            };
            final GnssStatus.Callback fcb = cb;
            try {
                if (Build.VERSION.SDK_INT >= 30) m.registerGnssStatusCallback(h::post, fcb);
                else m.registerGnssStatusCallback(fcb, h);
            } catch (Throwable t) { emit("W satellite status unavailable: " + t.getMessage()); }
        }
        // Keep the receiver running at up to 1 Hz (a GNSS chip asked for a 10 s minimum schedules fixes itself, often
        // 15-20 s apart) and report the newest fix on a strict interval clock. minDistance 0: report while standing still.
        m.requestLocationUpdates(provider, Math.min(intervalMs, 1000L), 0f, listener, looper);
        final GnssStatus.Callback fcb2 = cb;
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            try { m.removeUpdates(listener); } catch (Throwable ignored) { }
            try { if (fcb2 != null) m.unregisterGnssStatusCallback(fcb2); } catch (Throwable ignored) { }
        }));
        JSONObject info = new JSONObject();
        info.put("provider", provider);
        info.put("interval_ms", intervalMs);
        info.put("enabled", m.isProviderEnabled(provider));
        try { if (Build.VERSION.SDK_INT >= 28) info.put("location_enabled", m.isLocationEnabled()); } catch (Throwable ignored) { }
        emit("I " + info);
        int ppid = parentPid();
        long next = SystemClock.elapsedRealtime() + intervalMs;
        long lastSentNanos = 0;
        boolean firstSent = false;
        while (true) {
            SystemClock.sleep(250);
            long now = SystemClock.elapsedRealtime();
            Location l;
            synchronized (GpsTest.class) { l = latest; }
            // the first fix goes out as soon as it exists, later ones on the interval clock
            if ((!firstSent && l != null) || now >= next) {
                if (l != null && l.getElapsedRealtimeNanos() != lastSentNanos) {
                    lastSentNanos = l.getElapsedRealtimeNanos();
                    firstSent = true;
                    try { emit("L " + loc(l)); } catch (Throwable t) { emit("E " + t); }
                } else if (now >= next) {
                    emit("N");                              // this slot had no new fix
                }
                if (now >= next) next += intervalMs * Math.max(1, (now - next) / intervalMs + 1);
                else next = now + intervalMs;
            }
            if (now / 1000 != (now - 250) / 1000) {
                emit("K");
                if (ppid > 1 && parentPid() != ppid) {
                    try { m.removeUpdates(listener); } catch (Throwable ignored) { }
                    System.exit(0);
                }
            }
        }
    }
}
