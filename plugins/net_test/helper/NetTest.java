// Network hardware helper for the AndroUnderTheHood "Cell, Wi-Fi & Bluetooth" plugin.
// Runs on the device with:  CLASSPATH=nettest.jar app_process / NetTest <command> ...
//
//   list                 -> JSON: modem/SIMs/cells, Wi-Fi hardware + connection + saved networks, Bluetooth adapter +
//                           paired devices, active network (IP, DNS, transports)
//   watch <ms> [token]   -> every <ms>: "D {json snapshot}" (cells, signal, service state, Wi-Fi link); "K" keep-alive
//   wifi-scan            -> JSON list of access points (triggers a fresh scan, waits up to 8 s)
//   bt-scan <seconds>    -> JSON list of Bluetooth LE devices heard
//   wifi-disconnect | wifi-reconnect | wifi-select <netId> | bt-connect <addr> | bt-disconnect <addr>  -> "OK ..." / "E ..."
//
// Built against android.jar with javac --release 11 and d8 (see build_helper.sh).

import android.annotation.SuppressLint;
import android.app.Application;
import android.app.Instrumentation;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothClass;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.pm.ApplicationInfo;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.RouteInfo;
import android.net.wifi.ScanResult.InformationElement;
import android.net.wifi.WifiConfiguration;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.telephony.CellIdentity;
import android.telephony.CellIdentityCdma;
import android.telephony.CellIdentityGsm;
import android.telephony.CellIdentityLte;
import android.telephony.CellIdentityNr;
import android.telephony.CellIdentityTdscdma;
import android.telephony.CellIdentityWcdma;
import android.telephony.CellInfo;
import android.telephony.CellInfoCdma;
import android.telephony.CellInfoGsm;
import android.telephony.CellInfoLte;
import android.telephony.CellInfoNr;
import android.telephony.CellInfoTdscdma;
import android.telephony.CellInfoWcdma;
import android.telephony.CellSignalStrength;
import android.telephony.CellSignalStrengthCdma;
import android.telephony.CellSignalStrengthGsm;
import android.telephony.CellSignalStrengthLte;
import android.telephony.CellSignalStrengthNr;
import android.telephony.CellSignalStrengthTdscdma;
import android.telephony.CellSignalStrengthWcdma;
import android.telephony.NetworkRegistrationInfo;
import android.telephony.ServiceState;
import android.telephony.SignalStrength;
import android.telephony.SubscriptionInfo;
import android.telephony.SubscriptionManager;
import android.telephony.TelephonyManager;

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
import java.net.InetAddress;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

@SuppressLint({"PrivateApi", "DiscouragedPrivateApi", "SoonBlockedPrivateApi", "NewApi", "MissingPermission",
        "HardwareIds", "WrongConstant"})
public final class NetTest {
    static String PKG = "com.android.shell";      // "android" when running as the system user (uid 1000)
    static Context ctx;
    static PrintStream out;
    static Handler handler;

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
        // Apps get the telephony / Bluetooth / connectivity service managers registered at startup; app_process
        // does not, so TelephonyManager & co. would silently return nothing.
        try {
            Method im = at.getDeclaredMethod("initializeMainlineModules");
            im.setAccessible(true);
            im.invoke(null);
        } catch (Throwable t) {
            System.err.println("initializeMainlineModules: " + t);
        }
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

    /** System services come bound to the system context (package "android"); point them at ours. */
    static <T> T rebind(T svc) {
        if (svc == null) return null;
        for (Class<?> c = svc.getClass(); c != null && c != Object.class; c = c.getSuperclass()) {
            for (Field f : c.getDeclaredFields()) {
                if (f.getName().equals("mContext") && Context.class.isAssignableFrom(f.getType())) {
                    try { f.setAccessible(true); f.set(svc, ctx); } catch (Throwable ignored) { }
                }
            }
        }
        return svc;
    }

    static TelephonyManager tm() { return rebind((TelephonyManager) ctx.getSystemService(Context.TELEPHONY_SERVICE)); }
    static SubscriptionManager sm() { return rebind((SubscriptionManager) ctx.getSystemService(Context.TELEPHONY_SUBSCRIPTION_SERVICE)); }
    static WifiManager wm() { return rebind((WifiManager) ctx.getSystemService(Context.WIFI_SERVICE)); }
    static ConnectivityManager cm() { return rebind((ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE)); }

    static BluetoothAdapter bt() {
        try {   // a manager built on our context carries our attribution (package + uid)
            Constructor<BluetoothManager> k = BluetoothManager.class.getDeclaredConstructor(Context.class);
            k.setAccessible(true);
            BluetoothAdapter a = k.newInstance(ctx).getAdapter();
            if (a != null) return a;
        } catch (Throwable ignored) { }
        BluetoothManager m = (BluetoothManager) ctx.getSystemService(Context.BLUETOOTH_SERVICE);
        return m == null ? null : m.getAdapter();
    }

    public static void main(String[] args) {
        out = new PrintStream(new BufferedOutputStream(new FileOutputStream(FileDescriptor.out), 1 << 15), false);
        try {
            if (Looper.getMainLooper() == null) Looper.prepareMainLooper();
        } catch (Throwable ignored) { }
        try {
            if (Process.myUid() == 1000) PKG = "android";
            ctx = makeContext();
            HandlerThread ht = new HandlerThread("net");
            ht.start();
            handler = new Handler(ht.getLooper());
            String cmd = args.length > 0 ? args[0] : "";
            switch (cmd) {
                case "list": println(list().toString()); break;
                case "watch": watch(Long.parseLong(args[1])); break;
                case "wifi-scan": println(wifiScan().toString()); break;
                case "bt-scan": {
                    // BluetoothLeScanner delivers results on the main looper: scan on a worker, pump the main looper
                    final int secs = Integer.parseInt(args[1]);
                    new Thread(() -> {
                        try { println(btScan(secs).toString()); }
                        catch (Throwable t) { println("E " + t.getClass().getSimpleName() + ": " + t.getMessage()); }
                        out.flush();
                        System.exit(0);
                    }).start();
                    Looper.loop();
                    break;
                }
                case "wifi-disconnect": println(wm().disconnect() ? "OK disconnected" : "E Wi-Fi refused to disconnect"); break;
                case "wifi-reconnect": println(wm().reconnect() ? "OK reconnecting" : "E Wi-Fi refused to reconnect"); break;
                case "wifi-select": {        // connect to a saved network by id (needs the system user)
                    int id = Integer.parseInt(args[1]);
                    boolean ok = wm().enableNetwork(id, true);
                    if (ok) wm().reconnect();
                    println(ok ? "OK connecting to saved network " + id : "E Wi-Fi refused (needs root / system user)");
                    break;
                }
                case "bt-connect": println(btConnect(args[1], true)); break;
                case "bt-disconnect": println(btConnect(args[1], false)); break;
                default:
                    System.err.println("usage: NetTest list | watch <ms> | wifi-scan | bt-scan <s> | wifi-disconnect | wifi-reconnect | bt-connect <addr> | bt-disconnect <addr>");
                    System.exit(2);
            }
            out.flush();
            System.exit(0);
        } catch (Throwable t) {
            out.println("E " + t.getClass().getSimpleName() + ": " + t.getMessage());
            out.flush();
            t.printStackTrace();
            System.exit(1);
        }
    }

    static synchronized void println(String s) {
        out.print(s);
        out.print('\n');
        out.flush();
        if (out.checkError()) System.exit(0);       // PC closed the stream
    }

    // ------------------------------------------------------------------ small helpers
    static Object call(Object o, String name, Object... args) {
        try {
            for (Method m : o.getClass().getMethods()) {
                if (m.getName().equals(name) && m.getParameterCount() == args.length) return m.invoke(o, args);
            }
            for (Class<?> c = o.getClass(); c != null; c = c.getSuperclass()) {
                for (Method m : c.getDeclaredMethods()) {
                    if (m.getName().equals(name) && m.getParameterCount() == args.length) {
                        m.setAccessible(true);
                        return m.invoke(o, args);
                    }
                }
            }
        } catch (Throwable ignored) { }
        return null;
    }

    static void put(JSONObject o, String k, Object v) {
        try { if (v != null) o.put(k, v); } catch (Throwable ignored) { }
    }

    static boolean valid(int v) { return v != Integer.MAX_VALUE && v != CellInfo.UNAVAILABLE; }

    static void putI(JSONObject o, String k, int v) { if (valid(v)) put(o, k, v); }

    static void putL(JSONObject o, String k, long v) { if (v != Long.MAX_VALUE && v != CellInfo.UNAVAILABLE_LONG) put(o, k, v); }

    static String netTypeName(int t) {
        Object n = null;
        try {
            Method m = TelephonyManager.class.getDeclaredMethod("getNetworkTypeName", int.class);
            m.setAccessible(true);
            n = m.invoke(null, t);
        } catch (Throwable ignored) { }
        return n != null ? n.toString() : String.valueOf(t);
    }

    /** All public no-argument boolean "is…" / "has…" getters, e.g. Wi-Fi and Bluetooth feature flags. */
    static JSONObject flags(Object o, String... prefixes) {
        JSONObject r = new JSONObject();
        if (o == null) return r;
        for (Method m : o.getClass().getMethods()) {
            if (m.getParameterCount() != 0) continue;
            String n = m.getName();
            boolean ok = false;
            for (String p : prefixes) if (n.startsWith(p)) ok = true;
            if (!ok || n.equals("isEnabled") || n.equals("isWifiEnabled")) continue;
            Class<?> rt = m.getReturnType();
            if (rt != boolean.class && !(rt == int.class && n.endsWith("Supported"))) continue;
            try {
                Object v = m.invoke(o);
                if (rt == int.class) v = ((Integer) v) == 10;     // BluetoothStatusCodes.FEATURE_SUPPORTED
                r.put(n, v);
            } catch (Throwable ignored) { }
        }
        return r;
    }

    // ------------------------------------------------------------------ cellular
    static JSONObject cell(CellInfo ci) {
        JSONObject o = new JSONObject();
        put(o, "registered", ci.isRegistered());
        if (Build.VERSION.SDK_INT >= 28) put(o, "status", ci.getCellConnectionStatus());   // 1 primary, 2 secondary
        CellIdentity id = Build.VERSION.SDK_INT >= 28 ? ci.getCellIdentity() : null;
        CellSignalStrength ss = Build.VERSION.SDK_INT >= 30 ? ci.getCellSignalStrength() : null;
        if (ci instanceof CellInfoLte) {
            CellIdentityLte i = ((CellInfoLte) ci).getCellIdentity();
            CellSignalStrengthLte s = ((CellInfoLte) ci).getCellSignalStrength();
            put(o, "type", "LTE");
            putI(o, "pci", i.getPci()); putI(o, "tac", i.getTac()); putI(o, "ci", i.getCi());
            if (Build.VERSION.SDK_INT >= 24) putI(o, "arfcn", i.getEarfcn());
            if (Build.VERSION.SDK_INT >= 28) putI(o, "bw_khz", i.getBandwidth());
            if (Build.VERSION.SDK_INT >= 30) { JSONArray b = new JSONArray(); for (int x : i.getBands()) b.put(x); put(o, "bands", b); }
            putI(o, "rsrp", s.getRsrp()); putI(o, "rsrq", s.getRsrq()); putI(o, "rssnr", s.getRssnr());
            if (Build.VERSION.SDK_INT >= 29) putI(o, "rssi", s.getRssi());
            putI(o, "cqi", s.getCqi()); putI(o, "ta", s.getTimingAdvance());
            ss = s;
        } else if (Build.VERSION.SDK_INT >= 29 && ci instanceof CellInfoNr) {
            CellIdentityNr i = (CellIdentityNr) ((CellInfoNr) ci).getCellIdentity();
            CellSignalStrengthNr s = (CellSignalStrengthNr) ((CellInfoNr) ci).getCellSignalStrength();
            put(o, "type", "NR");
            putI(o, "pci", i.getPci()); putI(o, "tac", i.getTac()); putL(o, "ci", i.getNci()); putI(o, "arfcn", i.getNrarfcn());
            if (Build.VERSION.SDK_INT >= 30) { JSONArray b = new JSONArray(); for (int x : i.getBands()) b.put(x); put(o, "bands", b); }
            putI(o, "rsrp", s.getSsRsrp()); putI(o, "rsrq", s.getSsRsrq()); putI(o, "sinr", s.getSsSinr());
            putI(o, "csi_rsrp", s.getCsiRsrp()); putI(o, "csi_rsrq", s.getCsiRsrq()); putI(o, "csi_sinr", s.getCsiSinr());
            if (Build.VERSION.SDK_INT >= 34) putI(o, "ta", s.getTimingAdvanceMicros());
            ss = s;
        } else if (ci instanceof CellInfoWcdma) {
            CellIdentityWcdma i = ((CellInfoWcdma) ci).getCellIdentity();
            CellSignalStrengthWcdma s = ((CellInfoWcdma) ci).getCellSignalStrength();
            put(o, "type", "WCDMA");
            putI(o, "psc", i.getPsc()); putI(o, "lac", i.getLac()); putI(o, "ci", i.getCid());
            if (Build.VERSION.SDK_INT >= 24) putI(o, "arfcn", i.getUarfcn());
            if (Build.VERSION.SDK_INT >= 30) putI(o, "rscp", s.getDbm());
            if (Build.VERSION.SDK_INT >= 30) putI(o, "ecno", s.getEcNo());
            ss = s;
        } else if (ci instanceof CellInfoGsm) {
            CellIdentityGsm i = ((CellInfoGsm) ci).getCellIdentity();
            CellSignalStrengthGsm s = ((CellInfoGsm) ci).getCellSignalStrength();
            put(o, "type", "GSM");
            putI(o, "lac", i.getLac()); putI(o, "ci", i.getCid());
            if (Build.VERSION.SDK_INT >= 24) { putI(o, "arfcn", i.getArfcn()); putI(o, "bsic", i.getBsic()); }
            if (Build.VERSION.SDK_INT >= 29) putI(o, "rssi", s.getRssi());
            if (Build.VERSION.SDK_INT >= 26) putI(o, "ta", s.getTimingAdvance());
            ss = s;
        } else if (Build.VERSION.SDK_INT >= 29 && ci instanceof CellInfoTdscdma) {
            CellIdentityTdscdma i = ((CellInfoTdscdma) ci).getCellIdentity();
            put(o, "type", "TD-SCDMA");
            putI(o, "cpid", i.getCpid()); putI(o, "lac", i.getLac()); putI(o, "ci", i.getCid()); putI(o, "arfcn", i.getUarfcn());
            ss = ((CellInfoTdscdma) ci).getCellSignalStrength();
            putI(o, "rscp", ((CellSignalStrengthTdscdma) ss).getRscp());
        } else if (ci instanceof CellInfoCdma) {
            CellIdentityCdma i = ((CellInfoCdma) ci).getCellIdentity();
            put(o, "type", "CDMA");
            putI(o, "sid", i.getSystemId()); putI(o, "nid", i.getNetworkId()); putI(o, "bsid", i.getBasestationId());
            ss = ((CellInfoCdma) ci).getCellSignalStrength();
            putI(o, "dbm", ((CellSignalStrengthCdma) ss).getCdmaDbm());
        }
        if (id != null) {
            if (Build.VERSION.SDK_INT >= 28) {
                String mcc = (String) call(id, "getMccString"), mnc = (String) call(id, "getMncString");
                if (mcc != null) put(o, "mcc", mcc);
                if (mnc != null) put(o, "mnc", mnc);
                CharSequence op = id.getOperatorAlphaLong();
                if (op != null) put(o, "operator", op.toString());
            }
        }
        if (ss != null) {
            put(o, "level", ss.getLevel());
            putI(o, "dbm", ss.getDbm());
            putI(o, "asu", ss.getAsuLevel());
        }
        return o;
    }

    static List<CellInfo> freshCells(TelephonyManager t) {
        if (Build.VERSION.SDK_INT >= 29) {
            final CountDownLatch done = new CountDownLatch(1);
            final List<List<CellInfo>> box = new ArrayList<>();
            try {
                t.requestCellInfoUpdate(handler::post, new TelephonyManager.CellInfoCallback() {
                    @Override public void onCellInfo(List<CellInfo> l) { box.add(l); done.countDown(); }
                    @Override public void onError(int e, Throwable d) { done.countDown(); }
                });
                done.await(3, TimeUnit.SECONDS);
                if (!box.isEmpty() && box.get(0) != null) return box.get(0);
            } catch (Throwable ignored) { }
        }
        try { return t.getAllCellInfo(); } catch (Throwable e) { return null; }
    }

    static JSONObject subState(TelephonyManager t, boolean fresh) {
        JSONObject o = new JSONObject();
        try { put(o, "operator", t.getNetworkOperatorName()); } catch (Throwable ignored) { }
        try { put(o, "plmn", t.getNetworkOperator()); } catch (Throwable ignored) { }
        try { put(o, "country", t.getNetworkCountryIso()); } catch (Throwable ignored) { }
        try { int d = t.getDataNetworkType(); put(o, "data_type", netTypeName(d)); } catch (Throwable ignored) { }
        try { int v = t.getVoiceNetworkType(); put(o, "voice_type", netTypeName(v)); } catch (Throwable ignored) { }
        try { put(o, "data_state", t.getDataState()); } catch (Throwable ignored) { }
        try { put(o, "data_enabled", t.isDataEnabled()); } catch (Throwable ignored) { }
        try { put(o, "roaming", t.isNetworkRoaming()); } catch (Throwable ignored) { }
        try {
            ServiceState s = t.getServiceState();
            if (s != null) {
                put(o, "service", s.getState());
                put(o, "manual_selection", s.getIsManualSelection());
                if (Build.VERSION.SDK_INT >= 28) {
                    JSONArray bw = new JSONArray();
                    for (int b : s.getCellBandwidths()) bw.put(b);
                    put(o, "cell_bandwidths", bw);
                    putI(o, "channel", s.getChannelNumber());
                    put(o, "duplex", s.getDuplexMode());
                }
                String str = s.toString();
                java.util.regex.Matcher m = java.util.regex.Pattern.compile("nrState=(\\w+)").matcher(str);
                if (m.find()) put(o, "nr_state", m.group(1));
                m = java.util.regex.Pattern.compile("isUsingCarrierAggregation ?= ?(\\w+)").matcher(str);
                if (m.find()) put(o, "carrier_aggregation", Boolean.parseBoolean(m.group(1)));
                m = java.util.regex.Pattern.compile("mIsNrAvailable ?= ?(\\w+)|isNrAvailable ?= ?(\\w+)").matcher(str);
                if (m.find()) put(o, "nr_available", Boolean.parseBoolean(m.group(1) != null ? m.group(1) : m.group(2)));
                if (Build.VERSION.SDK_INT >= 30) {
                    JSONArray regs = new JSONArray();
                    for (NetworkRegistrationInfo r : s.getNetworkRegistrationInfoList()) {
                        JSONObject x = new JSONObject();
                        put(x, "domain", r.getDomain() == NetworkRegistrationInfo.DOMAIN_CS ? "voice" : r.getDomain() == NetworkRegistrationInfo.DOMAIN_PS ? "data" : "voice+data");
                        put(x, "transport", r.getTransportType() == 2 ? "Wi-Fi (IWLAN)" : "cellular");
                        put(x, "tech", netTypeName(r.getAccessNetworkTechnology()));
                        put(x, "registered", r.isRegistered());
                        put(x, "roaming", r.isRoaming());
                        Object st = call(r, "getRegistrationState");
                        if (st != null) put(x, "state", st);
                        regs.put(x);
                    }
                    put(o, "registrations", regs);
                }
            }
        } catch (Throwable e) { put(o, "service_error", String.valueOf(e.getMessage())); }
        try {
            SignalStrength s = t.getSignalStrength();
            if (s != null) {
                put(o, "level", s.getLevel());
                if (Build.VERSION.SDK_INT >= 29) {
                    JSONArray a = new JSONArray();
                    for (CellSignalStrength c : s.getCellSignalStrengths()) {
                        JSONObject x = new JSONObject();
                        put(x, "kind", c.getClass().getSimpleName().replace("CellSignalStrength", ""));
                        put(x, "level", c.getLevel());
                        putI(x, "dbm", c.getDbm());
                        a.put(x);
                    }
                    put(o, "signals", a);
                }
            }
        } catch (Throwable ignored) { }
        try {
            List<CellInfo> cells = fresh ? freshCells(t) : t.getAllCellInfo();
            JSONArray a = new JSONArray();
            if (cells != null) for (CellInfo c : cells) a.put(cell(c));
            put(o, "cells", a);
        } catch (Throwable e) { put(o, "cells_error", String.valueOf(e.getMessage())); }
        return o;
    }

    static JSONObject modem() {
        JSONObject o = new JSONObject();
        TelephonyManager t = tm();
        try { put(o, "phone_type", t.getPhoneType()); } catch (Throwable ignored) { }
        try { if (Build.VERSION.SDK_INT >= 30) { put(o, "active_modems", t.getActiveModemCount()); put(o, "supported_modems", t.getSupportedModemCount()); } } catch (Throwable ignored) { }
        try { put(o, "sim_slots", Build.VERSION.SDK_INT >= 23 ? t.getPhoneCount() : 1); } catch (Throwable ignored) { }
        try {
            Object fam = call(t, "getSupportedRadioAccessFamily");
            if (fam instanceof Long) put(o, "radio_family", (long) (Long) fam);
        } catch (Throwable ignored) { }
        Object hal = call(t, "getRadioHalVersion");
        if (hal != null) put(o, "radio_hal", hal.toString());
        try { put(o, "multisim_config", String.valueOf(call(t, "getMultiSimConfiguration"))); } catch (Throwable ignored) { }
        try { if (Build.VERSION.SDK_INT >= 26) put(o, "concurrent_voice_data", t.isConcurrentVoiceAndDataSupported()); } catch (Throwable ignored) { }
        try { if (Build.VERSION.SDK_INT >= 22) put(o, "voice_capable", t.isVoiceCapable()); } catch (Throwable ignored) { }
        try { if (Build.VERSION.SDK_INT >= 26) put(o, "sms_capable", t.isSmsCapable()); } catch (Throwable ignored) { }
        try { if (Build.VERSION.SDK_INT >= 28) put(o, "world_phone", t.isWorldPhone()); } catch (Throwable ignored) { }
        return o;
    }

    static JSONArray sims(boolean fresh) {
        JSONArray a = new JSONArray();
        TelephonyManager base = tm();
        List<SubscriptionInfo> subs = null;
        try { subs = sm().getActiveSubscriptionInfoList(); } catch (Throwable e) { /* no permission */ }
        if (subs == null || subs.isEmpty()) {
            JSONObject o = subState(base, fresh);
            put(o, "slot", 0);
            put(o, "sub_id", -1);
            a.put(o);
            return a;
        }
        for (SubscriptionInfo s : subs) {
            TelephonyManager t = rebind(base.createForSubscriptionId(s.getSubscriptionId()));
            JSONObject o = subState(t, fresh);
            put(o, "slot", s.getSimSlotIndex());
            put(o, "sub_id", s.getSubscriptionId());
            put(o, "sim_name", String.valueOf(s.getDisplayName()));
            put(o, "carrier", String.valueOf(s.getCarrierName()));
            if (Build.VERSION.SDK_INT >= 29) { put(o, "sim_mcc", s.getMccString()); put(o, "sim_mnc", s.getMncString()); }
            put(o, "sim_country", s.getCountryIso());
            if (Build.VERSION.SDK_INT >= 28) put(o, "esim", s.isEmbedded());
            if (Build.VERSION.SDK_INT >= 29) put(o, "opportunistic", s.isOpportunistic());
            a.put(o);
        }
        return a;
    }

    // ------------------------------------------------------------------ Wi-Fi
    static JSONObject wifiLink(WifiManager w) {
        JSONObject o = new JSONObject();
        try {
            put(o, "enabled", w.isWifiEnabled());
            WifiInfo i = w.getConnectionInfo();
            if (i != null && i.getNetworkId() != -1 || (i != null && i.getRssi() > -127)) {
                put(o, "ssid", i.getSSID());
                put(o, "bssid", i.getBSSID());
                put(o, "rssi", i.getRssi());
                put(o, "link_mbps", i.getLinkSpeed());
                if (Build.VERSION.SDK_INT >= 29) { put(o, "tx_mbps", i.getTxLinkSpeedMbps()); put(o, "rx_mbps", i.getRxLinkSpeedMbps()); }
                if (Build.VERSION.SDK_INT >= 30) { put(o, "max_tx_mbps", i.getMaxSupportedTxLinkSpeedMbps()); put(o, "max_rx_mbps", i.getMaxSupportedRxLinkSpeedMbps()); put(o, "standard", i.getWifiStandard()); }
                put(o, "freq", i.getFrequency());
                put(o, "net_id", i.getNetworkId());
                put(o, "mac", i.getMacAddress());
                if (Build.VERSION.SDK_INT >= 31) put(o, "security", i.getCurrentSecurityType());
                put(o, "state", String.valueOf(i.getSupplicantState()));
                put(o, "hidden", i.getHiddenSSID());
                int ip = i.getIpAddress();
                if (ip != 0) put(o, "ip", (ip & 255) + "." + (ip >> 8 & 255) + "." + (ip >> 16 & 255) + "." + (ip >>> 24));
            }
        } catch (Throwable e) { put(o, "error", String.valueOf(e.getMessage())); }
        return o;
    }

    static JSONObject wifi() {
        JSONObject o = new JSONObject();
        WifiManager w = wm();
        if (w == null) { put(o, "absent", true); return o; }
        put(o, "link", wifiLink(w));
        JSONObject f = flags(w, "is");
        if (Build.VERSION.SDK_INT >= 30) {
            for (int s : new int[]{4, 5, 6, 7, 8}) {
                try { f.put("standard_" + s, w.isWifiStandardSupported(s)); } catch (Throwable ignored) { }
            }
        }
        put(o, "features", f);
        Object cc = call(w, "getCountryCode");
        if (cc != null) put(o, "country", cc.toString());
        try { if (Build.VERSION.SDK_INT >= 30) put(o, "max_signal_level", w.getMaxSignalLevel()); } catch (Throwable ignored) { }
        try {
            List<WifiConfiguration> saved = w.getConfiguredNetworks();
            JSONArray a = new JSONArray();
            if (saved != null) for (WifiConfiguration c : saved) {
                JSONObject x = new JSONObject();
                put(x, "id", c.networkId);
                put(x, "ssid", c.SSID);
                put(x, "hidden", c.hiddenSSID);
                // Android 13 lists a network once per security variant (e.g. WPA2 + its WPA3 upgrade); merge them
                String secName = "open";
                Object sec = call(c, "getSecurityParamsList");
                if (sec != null) {
                    java.util.regex.Matcher m = java.util.regex.Pattern.compile("Type: (\\d+)").matcher(sec.toString());
                    if (m.find()) secName = secTypeName(Integer.parseInt(m.group(1)));
                } else {
                    String k = c.allowedKeyManagement.toString();
                    secName = k.contains("8") ? "WPA3-SAE" : k.contains("1") ? "WPA2-PSK" : k.contains("2") ? "EAP" : "open";
                }
                JSONObject prev = null;
                for (int i = 0; i < a.length(); i++) if (a.getJSONObject(i).getInt("id") == c.networkId) prev = a.getJSONObject(i);
                if (prev != null) {
                    if (!prev.getString("security").contains(secName)) prev.put("security", prev.getString("security") + " / " + secName);
                } else {
                    put(x, "security", secName);
                    a.put(x);
                }
            }
            put(o, "saved", a);
        } catch (Throwable e) { put(o, "saved_error", String.valueOf(e.getMessage())); }
        return o;
    }

    static String secTypeName(int t) {
        switch (t) {
            case 0: return "open";
            case 1: return "WEP";
            case 2: return "WPA2-PSK";
            case 3: return "EAP";
            case 4: return "WPA3-SAE";
            case 5: return "EAP-Suite-B";
            case 6: return "OWE";
            case 7: return "WAPI-PSK";
            case 8: return "WAPI-CERT";
            case 9: return "WPA3-EAP";
            case 10: return "OSEN";
            case 11: return "Passpoint R1/R2";
            case 12: return "Passpoint R3";
            case 13: return "DPP";
            default: return "type " + t;
        }
    }

    static JSONArray wifiScan() throws Exception {
        WifiManager w = wm();
        long before = SystemClock.elapsedRealtime() * 1000;
        try { w.startScan(); } catch (Throwable ignored) { }
        List<android.net.wifi.ScanResult> res = null;
        for (int i = 0; i < 16; i++) {        // wait for fresh results (up to 8 s), else use what we have
            SystemClock.sleep(500);
            res = w.getScanResults();
            boolean fresh = false;
            if (res != null) for (android.net.wifi.ScanResult r : res) if (r.timestamp >= before) { fresh = true; break; }
            if (fresh && i >= 3) break;
        }
        JSONArray a = new JSONArray();
        if (res == null) return a;
        long now = SystemClock.elapsedRealtime() * 1000;
        for (android.net.wifi.ScanResult r : res) {
            JSONObject o = new JSONObject();
            put(o, "ssid", r.SSID);
            put(o, "bssid", r.BSSID);
            put(o, "freq", r.frequency);
            put(o, "level", r.level);
            put(o, "caps", r.capabilities);
            if (Build.VERSION.SDK_INT >= 23) { put(o, "width", r.channelWidth); put(o, "center0", r.centerFreq0); }
            if (Build.VERSION.SDK_INT >= 30) put(o, "standard", r.getWifiStandard());
            put(o, "age_s", Math.max(0, (now - r.timestamp) / 1000000));
            if (Build.VERSION.SDK_INT >= 23) put(o, "passpoint", r.isPasspointNetwork());
            a.put(o);
        }
        return a;
    }

    // ------------------------------------------------------------------ Bluetooth
    static String btType(int t) { return t == 1 ? "Classic" : t == 2 ? "LE" : t == 3 ? "Dual" : "Unknown"; }

    static String majorClass(int c) {
        switch (c) {
            case BluetoothClass.Device.Major.AUDIO_VIDEO: return "Audio/video";
            case BluetoothClass.Device.Major.COMPUTER: return "Computer";
            case BluetoothClass.Device.Major.HEALTH: return "Health";
            case BluetoothClass.Device.Major.IMAGING: return "Imaging";
            case BluetoothClass.Device.Major.NETWORKING: return "Networking";
            case BluetoothClass.Device.Major.PERIPHERAL: return "Peripheral";
            case BluetoothClass.Device.Major.PHONE: return "Phone";
            case BluetoothClass.Device.Major.TOY: return "Toy";
            case BluetoothClass.Device.Major.WEARABLE: return "Wearable";
            case BluetoothClass.Device.Major.UNCATEGORIZED: return "Uncategorized";
            default: return "Misc";
        }
    }

    static JSONObject bluetooth() {
        JSONObject o = new JSONObject();
        BluetoothAdapter a = bt();
        if (a == null) { put(o, "absent", true); return o; }
        try { put(o, "enabled", a.isEnabled()); put(o, "state", a.getState()); } catch (Throwable ignored) { }
        try { put(o, "name", a.getName()); } catch (Throwable ignored) { }
        try { put(o, "address", a.getAddress()); } catch (Throwable ignored) { }
        put(o, "features", flags(a, "isLe", "isMultiple", "isOffloaded", "isLeAudio"));
        try { if (Build.VERSION.SDK_INT >= 26) put(o, "max_adv_bytes", a.getLeMaximumAdvertisingDataLength()); } catch (Throwable ignored) { }
        Object prof = call(a, "getSupportedProfiles");
        if (prof != null) put(o, "profiles", prof.toString());
        Object maxAudio = call(a, "getMaxConnectedAudioDevices");
        if (maxAudio != null) put(o, "max_audio_devices", maxAudio);
        JSONArray devs = new JSONArray();
        try {
            for (BluetoothDevice d : a.getBondedDevices()) {
                JSONObject x = new JSONObject();
                put(x, "address", d.getAddress());
                try { put(x, "name", d.getName()); } catch (Throwable ignored) { }
                try { if (Build.VERSION.SDK_INT >= 30) put(x, "alias", d.getAlias()); } catch (Throwable ignored) { }
                try { put(x, "type", btType(d.getType())); } catch (Throwable ignored) { }
                try { BluetoothClass c = d.getBluetoothClass(); if (c != null) put(x, "class", majorClass(c.getMajorDeviceClass())); } catch (Throwable ignored) { }
                Object con = call(d, "isConnected");
                if (con != null) put(x, "connected", con);
                Object bat = call(d, "getBatteryLevel");
                if (bat instanceof Integer && (Integer) bat >= 0) put(x, "battery", bat);
                devs.put(x);
            }
        } catch (Throwable e) { put(o, "bonded_error", String.valueOf(e.getMessage())); }
        put(o, "bonded", devs);
        return o;
    }

    static JSONArray btScan(int secs) throws Exception {
        BluetoothAdapter a = bt();
        JSONArray arr = new JSONArray();
        if (a == null || !a.isEnabled()) throw new IllegalStateException("Bluetooth is off");
        BluetoothLeScanner sc = a.getBluetoothLeScanner();
        final Map<String, JSONObject> seen = new HashMap<>();
        ScanCallback cb = new ScanCallback() {
            @Override public void onScanResult(int type, ScanResult r) {
                synchronized (seen) {
                    String addr = r.getDevice().getAddress();
                    JSONObject x = seen.get(addr);
                    if (x == null) { x = new JSONObject(); seen.put(addr, x); put(x, "address", addr); put(x, "count", 0); }
                    try { x.put("count", x.getInt("count") + 1); } catch (Throwable ignored) { }
                    put(x, "rssi", r.getRssi());
                    if (r.getScanRecord() != null && r.getScanRecord().getDeviceName() != null) put(x, "name", r.getScanRecord().getDeviceName());
                    try { String n = r.getDevice().getName(); if (n != null) put(x, "name", n); } catch (Throwable ignored) { }
                    if (Build.VERSION.SDK_INT >= 26) { put(x, "connectable", r.isConnectable()); putI(x, "tx_power", r.getTxPower()); }
                    if (r.getScanRecord() != null && r.getScanRecord().getManufacturerSpecificData() != null
                            && r.getScanRecord().getManufacturerSpecificData().size() > 0) {
                        put(x, "manufacturer_id", r.getScanRecord().getManufacturerSpecificData().keyAt(0));
                    }
                }
            }
        };
        final int[] failed = {0};
        ScanCallback watch = cb;
        ScanCallback cb2 = new ScanCallback() {
            @Override public void onScanResult(int type, ScanResult r) { watch.onScanResult(type, r); }
            @Override public void onBatchScanResults(java.util.List<ScanResult> l) { for (ScanResult r : l) watch.onScanResult(0, r); }
            @Override public void onScanFailed(int code) { failed[0] = code; }
        };
        cb = cb2;
        android.bluetooth.le.ScanSettings st = new android.bluetooth.le.ScanSettings.Builder()
                .setScanMode(android.bluetooth.le.ScanSettings.SCAN_MODE_LOW_LATENCY).build();
        sc.startScan(null, st, cb);
        SystemClock.sleep(secs * 1000L);
        if (failed[0] != 0) throw new IllegalStateException("Bluetooth LE scan failed (code " + failed[0] + ")");
        try { sc.stopScan(cb); } catch (Throwable ignored) { }
        synchronized (seen) { for (JSONObject x : seen.values()) arr.put(x); }
        return arr;
    }

    static String btConnect(String addr, boolean connect) {
        BluetoothAdapter a = bt();
        if (a == null) return "E no Bluetooth adapter";
        BluetoothDevice d = a.getRemoteDevice(addr.toUpperCase());
        Object r = call(d, connect ? "connect" : "disconnect");
        if (r == null) return "E this Android version does not allow " + (connect ? "connecting" : "disconnecting") + " devices from here";
        return (r instanceof Integer && (Integer) r != 0) ? "E Bluetooth returned status " + r : "OK " + (connect ? "connecting" : "disconnecting") + " " + addr;
    }

    // ------------------------------------------------------------------ active network
    static JSONArray networks() {
        JSONArray a = new JSONArray();
        ConnectivityManager c = cm();
        if (c == null) return a;
        Network active = null;
        try { active = c.getActiveNetwork(); } catch (Throwable ignored) { }
        try {
            for (Network n : c.getAllNetworks()) {
                JSONObject o = new JSONObject();
                NetworkCapabilities nc = c.getNetworkCapabilities(n);
                LinkProperties lp = c.getLinkProperties(n);
                put(o, "default", n.equals(active));
                if (nc != null) {
                    JSONArray tr = new JSONArray();
                    String[] names = {"cellular", "wifi", "bluetooth", "ethernet", "vpn", "wifi-aware", "lowpan", "test", "usb", "thread", "satellite"};
                    for (int i = 0; i < names.length; i++) if (nc.hasTransport(i)) tr.put(names[i]);
                    put(o, "transports", tr);
                    put(o, "validated", nc.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED));
                    put(o, "internet", nc.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET));
                    put(o, "metered", !nc.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED));
                    put(o, "down_kbps", nc.getLinkDownstreamBandwidthKbps());
                    put(o, "up_kbps", nc.getLinkUpstreamBandwidthKbps());
                    if (Build.VERSION.SDK_INT >= 29) putI(o, "signal", nc.getSignalStrength());
                }
                if (lp != null) {
                    put(o, "iface", lp.getInterfaceName());
                    JSONArray ad = new JSONArray();
                    for (LinkAddress la : lp.getLinkAddresses()) ad.put(la.toString());
                    put(o, "addresses", ad);
                    JSONArray dns = new JSONArray();
                    for (InetAddress d : lp.getDnsServers()) dns.put(d.getHostAddress());
                    put(o, "dns", dns);
                    if (Build.VERSION.SDK_INT >= 28) { put(o, "private_dns", lp.isPrivateDnsActive()); put(o, "private_dns_server", lp.getPrivateDnsServerName()); }
                    for (RouteInfo r : lp.getRoutes()) if (r.isDefaultRoute() && r.getGateway() != null) { put(o, "gateway", r.getGateway().getHostAddress()); break; }
                    if (Build.VERSION.SDK_INT >= 29) put(o, "mtu", lp.getMtu());
                }
                a.put(o);
            }
        } catch (Throwable e) {
            JSONObject o = new JSONObject();
            put(o, "error", String.valueOf(e.getMessage()));
            a.put(o);
        }
        return a;
    }

    // ------------------------------------------------------------------ commands
    static JSONObject list() {
        JSONObject root = new JSONObject();
        put(root, "sdk", Build.VERSION.SDK_INT);
        put(root, "model", Build.MANUFACTURER + " " + Build.MODEL);
        put(root, "uid", Process.myUid());
        put(root, "modem", modem());
        put(root, "sims", sims(true));
        put(root, "wifi", wifi());
        put(root, "bluetooth", bluetooth());
        put(root, "networks", networks());
        return root;
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

    static void watch(long ms) {
        int ppid = parentPid();
        WifiManager w = wm();
        BluetoothAdapter a = bt();
        while (true) {
            long t0 = SystemClock.elapsedRealtime();
            JSONObject o = new JSONObject();
            put(o, "t", System.currentTimeMillis());
            put(o, "sims", sims(true));
            if (w != null) put(o, "wifi", wifiLink(w));
            if (a != null) { try { put(o, "bt_on", a.isEnabled()); } catch (Throwable ignored) { } }
            try {
                ConnectivityManager c = cm();
                NetworkCapabilities nc = c.getNetworkCapabilities(c.getActiveNetwork());
                if (nc != null) put(o, "default_transport", nc.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ? "wifi"
                        : nc.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) ? "cellular" : nc.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) ? "ethernet" : "other");
            } catch (Throwable ignored) { }
            println("D " + o);
            while (SystemClock.elapsedRealtime() - t0 < ms) {
                SystemClock.sleep(Math.min(1000, Math.max(50, ms - (SystemClock.elapsedRealtime() - t0))));
                println("K");
                if (ppid > 1 && parentPid() != ppid) System.exit(0);
            }
        }
    }
}
