/**
 * 日志脱敏：dsh 打印的 URL 带一次性 token，stdout/stderr 里也可能出现密钥。
 * 写 dsh.log 与向壳广播的 tail 之前统一过一遍，避免把凭据落盘或显示在失败页。
 *
 * 只做「值替换」，保留参数名，方便排障时仍能看出是哪类参数出问题。
 */
const SECRET_PARAM = /([?&](?:token|access_token|refresh_token|api_key|apikey|api-key|secret|password|passwd)=)[^&\s"']+/gi
const BEARER = /(Bearer\s+)[A-Za-z0-9._~+/=-]+/gi

export function redactSecrets(input) {
  if (typeof input !== 'string' || input.length === 0) return input
  return input.replace(SECRET_PARAM, '$1***').replace(BEARER, '$1***')
}
