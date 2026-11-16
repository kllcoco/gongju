package cn.llmjourney.dict;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 开机、或者 App 更新以后，把下一次闪卡提醒重新排上（系统会清掉旧的定时）。 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        String a = intent.getAction();
        if (Intent.ACTION_BOOT_COMPLETED.equals(a) || Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)) {
            FlashCore.reschedule(ctx);
        }
    }
}
