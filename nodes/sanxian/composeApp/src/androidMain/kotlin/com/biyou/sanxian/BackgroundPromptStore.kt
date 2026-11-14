package com.biyou.sanxian

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.first

/**
 * 后台限制提示的确认记录。
 *
 * 只记录「用户点过稍后」时的信号指纹（bgRestricted / Doze 白名单 / standby bucket），
 * 指纹不变就不再用强弹窗打扰；信号解除时由调用方清除。
 */
private val Context.bgPromptDataStore by preferencesDataStore(name = "sanxian_bg_prompt")

object BackgroundPromptStore {

    private val KEY_ACK = stringPreferencesKey("ack_fingerprint")

    suspend fun acknowledgedFingerprint(context: Context): String? =
        context.applicationContext.bgPromptDataStore.data.first()[KEY_ACK]

    suspend fun acknowledge(context: Context, fingerprint: String) {
        context.applicationContext.bgPromptDataStore.edit { it[KEY_ACK] = fingerprint }
    }

    suspend fun clear(context: Context) {
        context.applicationContext.bgPromptDataStore.edit { it.remove(KEY_ACK) }
    }
}
