// 共同入口只接受已知本地路由；跨应用使用页面导航以加载02应用。
export function loginTarget(value: string): string | null {
  if (!value.startsWith('/') || value.startsWith('//') || /[\\\r\n\t]/.test(value)) return null
  if (/%(?:2f|5c|0[ad])/i.test(value)) return null
  const path = value.split(/[?#]/, 1)[0]
  return /^(?:\/|\/operations\/(?:[a-zA-Z0-9_-]+\/?)?|\/(?:shop|cart|checkout|wallet|orders|workbench|service|knowledge)(?:\/[a-zA-Z0-9_-]+)?)$/.test(path)
    ? value : null
}
