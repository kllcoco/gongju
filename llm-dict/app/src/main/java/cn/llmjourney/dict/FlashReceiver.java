package cn.llmjourney.dict;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** 通知栏闪卡上的按钮（翻面、四档评分、划掉）和定时提醒都走这里。 */
public class FlashReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        String a = intent.getAction();
        if (a == null) return;
        long now = System.currentTimeMillis();
        JSONObject o = FlashCore.load(ctx);
        if (o == null) return;
        String id = intent.getStringExtra("id");
        try {
            if (FlashCore.A_ALARM.equals(a)) {
                FlashCore.next(ctx, o, now, false);
            } else if (FlashCore.A_FLIP.equals(a)) {
                if (id != null) FlashCore.post(ctx, o, id, true);
            } else if (FlashCore.A_GRADE.equals(a)) {
                int g = intent.getIntExtra("g", 0);
                JSONObject cs = o.optJSONObject("c");
                JSONObject c = (cs == null || id == null) ? null : cs.optJSONObject(id);
                if (c != null && g >= 1 && g <= 4) {
                    FlashCore.grade(c, g, now);
                    JSONArray log = o.optJSONArray("nlog");
                    if (log == null) {
                        log = new JSONArray();
                        o.put("nlog", log);
                    }
                    JSONObject e = new JSONObject();
                    e.put("k", "rv");
                    e.put("id", id);
                    e.put("g", g);
                    e.put("t", now);
                    e.put("from", "通知");
                    log.put(e);
                }
                FlashCore.next(ctx, o, now, true);
            } else if (FlashCore.A_DISMISS.equals(a)) {
                o.put("snooze", now + FlashCore.HOUR);
                FlashCore.save(ctx, o);
                FlashCore.schedule(ctx, o, now);
            }
        } catch (JSONException ignored) {
        }
    }
}
