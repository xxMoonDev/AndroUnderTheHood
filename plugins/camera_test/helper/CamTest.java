// Camera test helper for the AndroUnderTheHood "Camera test" plugin.
// Runs on the device with:  CLASSPATH=camtest.jar app_process / CamTest list|stream ...
//
//   list                                  -> JSON description of every camera that can be found
//   stream <id> <parent|-> <w> <h> <q>    -> binary packets on stdout: [type u8][len u32 BE][payload]
//                                            'I' info JSON, 'F' JPEG frame, 'E' error text
//
// Built against android.jar with javac --release 11 and converted with d8 (see build_helper.bat).

import android.annotation.SuppressLint;
import android.app.Application;
import android.app.Instrumentation;
import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.pm.ApplicationInfo;
import android.graphics.ImageFormat;
import android.graphics.Rect;
import android.graphics.YuvImage;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CameraMetadata;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.OutputConfiguration;
import android.hardware.camera2.params.SessionConfiguration;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.Process;
import android.util.Range;
import android.util.Size;
import android.util.SizeF;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executor;
import java.util.concurrent.TimeUnit;

@SuppressLint({"PrivateApi", "DiscouragedPrivateApi", "SoonBlockedPrivateApi", "MissingPermission", "NewApi"})
public final class CamTest {
    static final String PKG = "com.android.shell";
    static Object activityThread;
    static Class<?> atClass;

    // ------------------------------------------------------------------ fake context
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
        atClass = Class.forName("android.app.ActivityThread");
        Constructor<?> c = atClass.getDeclaredConstructor();
        c.setAccessible(true);
        activityThread = c.newInstance();
        setField(atClass, null, "sCurrentActivityThread", activityThread);
        try { setField(atClass, activityThread, "mSystemThread", true); } catch (Throwable ignored) { }
        if (Build.VERSION.SDK_INT >= 31) {
            try {
                Class<?> cc = Class.forName("android.app.ConfigurationController");
                Class<?> ati = Class.forName("android.app.ActivityThreadInternal");
                Constructor<?> k = cc.getDeclaredConstructor(ati);
                k.setAccessible(true);
                setField(atClass, activityThread, "mConfigurationController", k.newInstance(activityThread));
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
            setField(atClass, activityThread, "mBoundApplication", data);
        } catch (Throwable ignored) { }
        Method m = atClass.getDeclaredMethod("getSystemContext");
        m.setAccessible(true);
        FakeContext ctx = new FakeContext((Context) m.invoke(activityThread));
        try {
            Application app = Instrumentation.newApplication(Application.class, ctx);
            setField(atClass, activityThread, "mInitialApplication", app);
        } catch (Throwable ignored) { }
        return ctx;
    }

    static void setField(Class<?> cls, Object obj, String name, Object value) throws Exception {
        Field f = cls.getDeclaredField(name);
        f.setAccessible(true);
        f.set(obj, value);
    }

    static CameraManager manager() throws Exception {
        Constructor<CameraManager> k = CameraManager.class.getDeclaredConstructor(Context.class);
        k.setAccessible(true);
        return k.newInstance(makeContext());
    }

    // ------------------------------------------------------------------ main
    public static void main(String[] args) {
        try {
            if (Looper.getMainLooper() == null) Looper.prepareMainLooper();
        } catch (Throwable ignored) { }
        try {
            if (args.length >= 1 && args[0].equals("list")) {
                System.out.println(list(manager()).toString());
                System.out.flush();
                System.exit(0);
            } else if (args.length >= 6 && args[0].equals("stream")) {
                stream(manager(), args[1], args[2].equals("-") ? null : args[2],
                        Integer.parseInt(args[3]), Integer.parseInt(args[4]), Integer.parseInt(args[5]));
            } else {
                System.err.println("usage: CamTest list | stream <id> <parent|-> <w> <h> <quality>");
                System.exit(2);
            }
        } catch (Throwable t) {
            t.printStackTrace();
            System.exit(1);
        }
    }

    // ------------------------------------------------------------------ list
    static JSONObject list(CameraManager cm) throws Exception {
        JSONObject root = new JSONObject();
        root.put("sdk", Build.VERSION.SDK_INT);
        root.put("model", Build.MANUFACTURER + " " + Build.MODEL);
        root.put("uid", Process.myUid());
        try { root.put("camera1_count", android.hardware.Camera.getNumberOfCameras()); } catch (Throwable ignored) { }

        Map<String, JSONObject> found = new LinkedHashMap<>();
        Map<String, String> parentOf = new LinkedHashMap<>();
        List<String> official = new ArrayList<>();
        String listError = null;
        try { official.addAll(Arrays.asList(cm.getCameraIdList())); } catch (Throwable t) { listError = String.valueOf(t); }
        if (listError != null) root.put("list_error", listError);

        for (String id : official) add(cm, found, id, "listed", null);
        // Many vendors hide extra (aux/wide/macro/depth) sensors from getCameraIdList(); probe numeric ids.
        int misses = 0;
        for (int i = 0; i < 64 && misses < 24; i++) {
            String id = Integer.toString(i);
            if (found.containsKey(id)) { misses = 0; continue; }
            if (add(cm, found, id, "hidden", null)) misses = 0; else misses++;
        }
        // Physical sensors behind logical (multi-camera) devices.
        if (Build.VERSION.SDK_INT >= 28) {
            for (String id : new ArrayList<>(found.keySet())) {
                try {
                    Set<String> phys = cm.getCameraCharacteristics(id).getPhysicalCameraIds();
                    JSONArray arr = new JSONArray();
                    for (String p : phys) {
                        arr.put(p);
                        if (!found.containsKey(p)) add(cm, found, p, "physical", id);
                        else if (!parentOf.containsKey(p)) found.get(p).put("parent_alt", id);
                    }
                    if (arr.length() > 0) found.get(id).put("physical_ids", arr);
                } catch (Throwable ignored) { }
            }
        }
        JSONArray cams = new JSONArray();
        for (JSONObject o : found.values()) cams.put(o);
        root.put("cameras", cams);
        return root;
    }

    static boolean add(CameraManager cm, Map<String, JSONObject> found, String id, String source, String parent) {
        try {
            CameraCharacteristics ch = cm.getCameraCharacteristics(id);
            JSONObject o = describe(ch);
            o.put("id", id);
            o.put("source", source);
            if (parent != null) o.put("parent", parent);
            found.put(id, o);
            return true;
        } catch (Throwable t) {
            return false;
        }
    }

    static JSONObject describe(CameraCharacteristics ch) throws Exception {
        JSONObject o = new JSONObject();
        Integer facing = ch.get(CameraCharacteristics.LENS_FACING);
        o.put("facing", facing == null ? "unknown" : facing == CameraMetadata.LENS_FACING_FRONT ? "front"
                : facing == CameraMetadata.LENS_FACING_BACK ? "back" : "external");
        Integer orient = ch.get(CameraCharacteristics.SENSOR_ORIENTATION);
        o.put("orientation", orient == null ? 0 : orient);
        Integer lvl = ch.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL);
        String[] lvls = {"LIMITED", "FULL", "LEGACY", "LEVEL_3", "EXTERNAL"};
        o.put("level", lvl == null ? "?" : (lvl >= 0 && lvl < lvls.length ? lvls[lvl] : String.valueOf(lvl)));
        Size pa = ch.get(CameraCharacteristics.SENSOR_INFO_PIXEL_ARRAY_SIZE);
        if (pa != null) {
            o.put("pixel_array", pa.getWidth() + "x" + pa.getHeight());
            o.put("megapixels", Math.round(pa.getWidth() * (double) pa.getHeight() / 1e5) / 10.0);
        }
        SizeF ps = ch.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE);
        if (ps != null) o.put("sensor_mm", String.format("%.2fx%.2f", ps.getWidth(), ps.getHeight()));
        float[] fl = ch.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS);
        if (fl != null && fl.length > 0) o.put("focal_mm", fl[0]);
        float[] ap = ch.get(CameraCharacteristics.LENS_INFO_AVAILABLE_APERTURES);
        if (ap != null && ap.length > 0) o.put("aperture", ap[0]);
        Boolean flash = ch.get(CameraCharacteristics.FLASH_INFO_AVAILABLE);
        o.put("flash", flash != null && flash);
        Float mf = ch.get(CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE);
        o.put("autofocus", mf != null && mf > 0);
        int[] caps = ch.get(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES);
        JSONArray capArr = new JSONArray();
        boolean logical = false, depthOnly = true;
        if (caps != null) {
            for (int c : caps) {
                capArr.put(capName(c));
                if (c == 11) logical = true;                 // LOGICAL_MULTI_CAMERA
                if (c == 0) depthOnly = false;               // BACKWARD_COMPATIBLE
            }
        }
        o.put("capabilities", capArr);
        o.put("logical", logical);
        o.put("color", !depthOnly);
        Range<Integer>[] fps = ch.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES);
        int maxFps = 0;
        if (fps != null) for (Range<Integer> r : fps) maxFps = Math.max(maxFps, r.getUpper());
        o.put("max_fps", maxFps);
        StreamConfigurationMap map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        JSONArray sizes = new JSONArray();
        if (map != null) {
            Size[] s = map.getOutputSizes(ImageFormat.YUV_420_888);
            if (s != null) {
                List<Size> l = new ArrayList<>(Arrays.asList(s));
                Collections.sort(l, (a, b) -> Long.compare((long) b.getWidth() * b.getHeight(), (long) a.getWidth() * a.getHeight()));
                for (Size z : l) sizes.put(z.getWidth() + "x" + z.getHeight());
            }
        }
        o.put("sizes", sizes);
        return o;
    }

    static String capName(int c) {
        String[] n = {"BACKWARD_COMPATIBLE", "MANUAL_SENSOR", "MANUAL_POST_PROCESSING", "RAW", "PRIVATE_REPROCESSING",
                "READ_SENSOR_SETTINGS", "BURST_CAPTURE", "YUV_REPROCESSING", "DEPTH_OUTPUT", "HIGH_SPEED_VIDEO",
                "MOTION_TRACKING", "LOGICAL_MULTI_CAMERA", "MONOCHROME", "SECURE_IMAGE_DATA", "SYSTEM_CAMERA",
                "OFFLINE_PROCESSING", "ULTRA_HIGH_RESOLUTION_SENSOR", "REMOSAIC_REPROCESSING", "DYNAMIC_RANGE_TEN_BIT",
                "STREAM_USE_CASE", "COLOR_SPACE_PROFILES"};
        return c >= 0 && c < n.length ? n[c] : "CAP_" + c;
    }

    // ------------------------------------------------------------------ stream
    static OutputStream out;

    static synchronized void packet(char type, byte[] data) throws IOException {
        int n = data.length;
        out.write(new byte[]{(byte) type, (byte) (n >>> 24), (byte) (n >>> 16), (byte) (n >>> 8), (byte) n});
        out.write(data);
        out.flush();
    }

    static void fail(String msg) {
        try { packet('E', msg.getBytes("UTF-8")); } catch (Throwable ignored) { }
        System.err.println(msg);
        System.exit(1);
    }

    static final Object lock = new Object();
    static byte[] pending;       // latest NV21 frame waiting to be encoded
    static int pw, ph;

    static void stream(CameraManager cm, String id, String parent, int w, int h, int quality) throws Exception {
        out = new BufferedOutputStream(new FileOutputStream(FileDescriptor.out), 1 << 16);
        HandlerThread ht = new HandlerThread("camera");
        ht.start();
        final Handler handler = new Handler(ht.getLooper());

        String openId = parent != null && Build.VERSION.SDK_INT >= 28 ? parent : id;
        final ImageReader reader = ImageReader.newInstance(w, h, ImageFormat.YUV_420_888, 3);
        reader.setOnImageAvailableListener(r -> {
            Image img = null;
            try {
                img = r.acquireLatestImage();
                if (img == null) return;
                int iw = img.getWidth(), ih = img.getHeight();
                byte[] buf;
                synchronized (lock) { buf = pending; pending = null; }
                if (buf == null || buf.length != iw * ih * 3 / 2) buf = new byte[iw * ih * 3 / 2];
                toNV21(img, buf, iw, ih);
                synchronized (lock) { pending = buf; pw = iw; ph = ih; lock.notifyAll(); }
            } catch (Throwable t) {
                System.err.println("frame: " + t);
            } finally {
                if (img != null) img.close();
            }
        }, handler);

        final CameraDevice[] dev = new CameraDevice[1];
        final String[] err = new String[1];
        final CountDownLatch opened = new CountDownLatch(1);
        cm.openCamera(openId, new CameraDevice.StateCallback() {
            @Override public void onOpened(CameraDevice d) { dev[0] = d; opened.countDown(); }
            @Override public void onDisconnected(CameraDevice d) {
                err[0] = "camera disconnected (another app took it?)"; opened.countDown(); fail(err[0]);
            }
            @Override public void onError(CameraDevice d, int e) {
                String[] names = {"", "CAMERA_IN_USE", "MAX_CAMERAS_IN_USE", "CAMERA_DISABLED", "CAMERA_DEVICE", "CAMERA_SERVICE"};
                err[0] = "camera error " + (e > 0 && e < names.length ? names[e] : String.valueOf(e));
                opened.countDown(); fail(err[0]);
            }
        }, handler);
        if (!opened.await(15, TimeUnit.SECONDS)) fail("timed out opening camera " + openId);
        if (dev[0] == null) fail(err[0] == null ? "could not open camera" : err[0]);
        final CameraDevice cam = dev[0];
        Runtime.getRuntime().addShutdownHook(new Thread(() -> { try { cam.close(); } catch (Throwable ignored) { } }));

        final CaptureRequest.Builder rb = cam.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
        rb.addTarget(reader.getSurface());
        final CountDownLatch configured = new CountDownLatch(1);
        final CameraCaptureSession[] sess = new CameraCaptureSession[1];
        CameraCaptureSession.StateCallback cb = new CameraCaptureSession.StateCallback() {
            @Override public void onConfigured(CameraCaptureSession s) { sess[0] = s; configured.countDown(); }
            @Override public void onConfigureFailed(CameraCaptureSession s) { configured.countDown(); }
        };
        if (Build.VERSION.SDK_INT >= 28) {
            OutputConfiguration oc = new OutputConfiguration(reader.getSurface());
            if (parent != null) oc.setPhysicalCameraId(id);
            Executor ex = handler::post;
            cam.createCaptureSession(new SessionConfiguration(SessionConfiguration.SESSION_REGULAR,
                    Collections.singletonList(oc), ex, cb));
        } else {
            cam.createCaptureSession(Collections.singletonList(reader.getSurface()), cb, handler);
        }
        if (!configured.await(15, TimeUnit.SECONDS) || sess[0] == null)
            fail("the camera refused the stream configuration (" + w + "x" + h + ")");
        sess[0].setRepeatingRequest(rb.build(), null, handler);

        CameraCharacteristics ch = cm.getCameraCharacteristics(id);
        JSONObject info = new JSONObject();
        info.put("id", id);
        info.put("opened", openId);
        info.put("width", w);
        info.put("height", h);
        Integer o = ch.get(CameraCharacteristics.SENSOR_ORIENTATION);
        info.put("orientation", o == null ? 0 : o);
        Integer f = ch.get(CameraCharacteristics.LENS_FACING);
        info.put("front", f != null && f == CameraMetadata.LENS_FACING_FRONT);
        packet('I', info.toString().getBytes("UTF-8"));

        // Encoder loop: always encodes the newest frame, so a slow link just lowers the frame rate.
        ByteArrayOutputStream jpg = new ByteArrayOutputStream(1 << 17);
        while (true) {
            byte[] buf;
            int fw, fh;
            synchronized (lock) {
                while (pending == null) lock.wait(5000);
                buf = pending; fw = pw; fh = ph; pending = null;
            }
            jpg.reset();
            new YuvImage(buf, ImageFormat.NV21, fw, fh, null).compressToJpeg(new Rect(0, 0, fw, fh), quality, jpg);
            try {
                packet('F', jpg.toByteArray());
            } catch (IOException e) {        // PC closed the stream
                try { cam.close(); } catch (Throwable ignored) { }
                System.exit(0);
            }
            synchronized (lock) { if (pending == null) pending = buf; }   // reuse the buffer
        }
    }

    static void toNV21(Image img, byte[] out, int w, int h) {
        Image.Plane[] p = img.getPlanes();
        ByteBuffer y = p[0].getBuffer();
        int yrs = p[0].getRowStride();
        int pos = 0;
        if (yrs == w) {
            y.position(0);
            y.get(out, 0, w * h);
            pos = w * h;
        } else {
            for (int r = 0; r < h; r++) {
                y.position(r * yrs);
                y.get(out, pos, w);
                pos += w;
            }
        }
        ByteBuffer u = p[1].getBuffer(), v = p[2].getBuffer();
        int urs = p[1].getRowStride(), ups = p[1].getPixelStride();
        int vrs = p[2].getRowStride(), vps = p[2].getPixelStride();
        int cw = w / 2, chh = h / 2;
        byte[] vrow = new byte[vrs], urow = new byte[urs];
        for (int r = 0; r < chh; r++) {
            int vn = Math.min(vrs, v.capacity() - r * vrs), un = Math.min(urs, u.capacity() - r * urs);
            v.position(r * vrs); v.get(vrow, 0, vn);
            u.position(r * urs); u.get(urow, 0, un);
            for (int c = 0; c < cw; c++) {
                out[pos++] = vrow[c * vps];
                out[pos++] = urow[c * ups];
            }
        }
    }
}
