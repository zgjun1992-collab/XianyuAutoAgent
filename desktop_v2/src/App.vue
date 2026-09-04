<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'

const desktop = window.xianyuDesktop
const route = ref('workspace')
const sidebarCollapsed = ref(false)
const aiPanelOpen = ref(true)
const browserStage = ref(null)
const loading = ref(false)
const toast = reactive({ text: '', kind: 'ok', visible: false })
const appIdentity = reactive({ edition: 'V3.5', frontend_version: '', backend_version: '', build_commit: '' })
const snapshot = reactive({
  dashboard: { products: 0, enabled_products: 0, store_lists: 0, stores: 0, audits: {}, events: [] },
  products: [], store_lists: [], reviews: [], service: { status: 'stopped', message: '' },
  conversations: [], config: {}, policies: {}
})
const productDraft = reactive({ item_id: '', title: '', raw_text: '', enabled: true, ai_summary: '', structured: {}, source_update: {}, time_rules: [], store_lists: [], store_list_ids: [], skus: [], image_assets: [], platform_summary: '', thumbnail_url: '', image_urls: [], price: '', item_status: 'onsale', source_type: 'manual', sync_status: 'manual', manual_edited: false, last_synced_at: '', first_reply_enabled: true, first_reply_text: '', first_reply_manual: false, first_reply_generated_at: '', coupon_type: 'meituan', coupon_type_custom: '', coupon_instructions: '', custom_policy_enabled: false, custom_policy_raw: '', custom_policy_summary: '', order_notice_enabled: true })
const configDraft = reactive({ api_key: '', base_url: '', model: '', api_key_saved: false, cookie_saved: false, cookie_updated_at: '' })
const policyDraft = reactive({ reply_mode: 'review', global_system_prompt: '', max_reply_rounds: 25, conversation_reset_hours: 24, safe_fallback: '', manual_review_notice: '', price_fallback: '', refund_fallback: '', forbidden_phrases_text: '', order_payment_notice_enabled: true, aftersale_policy_raw: '', aftersale_policy_summary: '' })
const importDraft = reactive({ name: '', path: '', item_ids: [], text: '', preview: null })
const imageDraft = reactive({ id: null, source_path: '', name: '', purpose: '', trigger_words_text: '', reply_text: '', enabled: true })
const imagePreviews = reactive({})
const testDraft = reactive({ item_id: '', message: '', store_query: '', at: '' })
const testResult = ref(null)
const versions = ref([])
const showVersions = ref(false)
const productTab = ref('knowledge')
const productSearch = ref('')
const syncReport = ref(null)
const aftersaleProductId = ref('')
const storeListSelection = ref('')
const selectedStoreSkuKey = ref('')
const skuStoreDraft = reactive({ mode: 'inherit', list_ids: [] })
let policiesLoaded = false
let refreshTimer = null
let toastTimer = null
let resizeObserver = null
let removeEventListener = null

const navItems = [
  { id: 'dashboard', icon: '总', label: '工作总览' },
  { id: 'workspace', icon: '聊', label: '闲鱼工作台' },
  { id: 'products', icon: '品', label: '商品与知识' },
  { id: 'aftersale', icon: '策', label: '发货与退款政策' },
  { id: 'reviews', icon: '审', label: '回复审核', badge: () => snapshot.dashboard.audits.pending || 0 },
  { id: 'guardrails', icon: '盾', label: '客服约束' },
  { id: 'testing', icon: '测', label: '测试中心' },
  { id: 'settings', icon: '设', label: 'AI与连接' },
  { id: 'logs', icon: '录', label: '运行记录' }
]

const pageTitle = computed(() => navItems.find((item) => item.id === route.value)?.label || '闲鱼卡券 AI 客服')
const currentProduct = computed(() => snapshot.products.find((item) => item.item_id === productDraft.item_id) || snapshot.products[0] || null)
const filteredProducts = computed(() => {
  const query = productSearch.value.trim().toLowerCase()
  if (!query) return snapshot.products
  return snapshot.products.filter((item) => `${item.title} ${item.item_id}`.toLowerCase().includes(query))
})
const serviceActive = computed(() => ['starting', 'connected', 'reconnecting', 'stopping'].includes(snapshot.service.status))
const serviceLabel = computed(() => ({
  stopped: '客服已停止', starting: '正在启动', connected: '客服运行中', reconnecting: '正在重连', stopping: '正在停止', error: '连接异常'
}[snapshot.service.status] || snapshot.service.status))

function notify(text, kind = 'ok') {
  clearTimeout(toastTimer)
  toast.text = text
  toast.kind = kind
  toast.visible = true
  toastTimer = setTimeout(() => { toast.visible = false }, 3200)
}

async function call(method, path, body, quiet = false) {
  try {
    if (!quiet) loading.value = true
    const plainBody = body === undefined ? undefined : JSON.parse(JSON.stringify(body))
    return await desktop.backend(method, path, plainBody)
  } catch (error) {
    const raw = error.message || String(error)
    const message = raw.replace(/^Error invoking remote method '[^']+':\s*Error:\s*/i, '').replace(/^Error:\s*/i, '')
    notify(message, 'error')
    throw error
  } finally {
    if (!quiet) loading.value = false
  }
}

function applySnapshot(data) {
  Object.assign(snapshot, data)
  if (!productDraft.item_id && data.products.length) selectProduct(data.products[0])
  if (!testDraft.item_id && data.products.length) testDraft.item_id = data.products[0].item_id
  if (!policiesLoaded && data.policies) {
    policiesLoaded = true
    policyDraft.reply_mode = data.policies.reply_mode || 'review'
    policyDraft.global_system_prompt = data.policies.global_system_prompt || ''
    policyDraft.max_reply_rounds = Number(data.policies.max_reply_rounds || 25)
    policyDraft.conversation_reset_hours = Number(data.policies.conversation_reset_hours || 24)
    policyDraft.safe_fallback = data.policies.safe_fallback || ''
    policyDraft.manual_review_notice = data.policies.manual_review_notice || ''
    policyDraft.price_fallback = data.policies.price_fallback || ''
    policyDraft.refund_fallback = data.policies.refund_fallback || ''
    policyDraft.order_payment_notice_enabled = data.policies.order_payment_notice_enabled !== false
    policyDraft.aftersale_policy_raw = data.policies.aftersale_policy_raw || ''
    policyDraft.aftersale_policy_summary = data.policies.aftersale_policy_summary || ''
    policyDraft.forbidden_phrases_text = (data.policies.forbidden_phrases || []).join('\n')
  }
  if (!aftersaleProductId.value && data.products.length) aftersaleProductId.value = data.products[0].item_id
}

async function refresh(quiet = true) {
  const data = await call('GET', '/snapshot', undefined, quiet)
  applySnapshot(data)
}

function selectProduct(product) {
  Object.assign(productDraft, {
    item_id: product.item_id,
    title: product.title || '',
    raw_text: product.raw_text || '',
    enabled: Boolean(product.enabled),
    ai_summary: product.ai_summary || '',
    structured: product.structured || {},
    source_update: product.source_update || {},
    time_rules: product.time_rules || [],
    store_lists: product.store_lists || [],
    store_list_ids: (product.store_lists || []).map((item) => item.id),
    skus: product.skus || [],
    image_assets: product.image_assets || [],
    platform_summary: product.platform_summary || '',
    thumbnail_url: product.thumbnail_url || '',
    image_urls: product.image_urls || [],
    price: product.price || '',
    item_status: product.item_status || 'onsale',
    source_type: product.source_type || 'manual',
    sync_status: product.sync_status || 'manual',
    manual_edited: Boolean(product.manual_edited),
    last_synced_at: product.last_synced_at || '',
    first_reply_enabled: product.first_reply_enabled !== false && product.first_reply_enabled !== 0,
    first_reply_text: product.first_reply_text || '',
    first_reply_manual: Boolean(product.first_reply_manual),
    first_reply_generated_at: product.first_reply_generated_at || '',
    coupon_type: product.coupon_type || 'meituan',
    coupon_type_custom: product.coupon_type_custom || '',
    coupon_instructions: product.coupon_instructions || '',
    custom_policy_enabled: Boolean(product.custom_policy_enabled),
    custom_policy_raw: product.custom_policy_raw || '',
    custom_policy_summary: product.custom_policy_summary || '',
    order_notice_enabled: product.order_notice_enabled !== false && product.order_notice_enabled !== 0
  })
  const selected = (product.skus || []).find((item) => item.sku_key === selectedStoreSkuKey.value) || (product.skus || [])[0]
  selectStoreSku(selected)
  importDraft.item_ids = [product.item_id]
  testDraft.item_id = product.item_id
  loadImagePreviews(product.image_assets || [])
}

function openProductKnowledge(product) {
  selectProduct(product)
  productTab.value = 'knowledge'
}

function newProduct() {
  Object.assign(productDraft, { item_id: '', title: '', raw_text: '', enabled: true, ai_summary: '', structured: {}, source_update: {}, time_rules: [], store_lists: [], store_list_ids: [], skus: [], image_assets: [], platform_summary: '', thumbnail_url: '', image_urls: [], price: '', item_status: 'onsale', source_type: 'manual', sync_status: 'manual', manual_edited: false, last_synced_at: '', first_reply_enabled: true, first_reply_text: '', first_reply_manual: false, first_reply_generated_at: '', coupon_type: 'meituan', coupon_type_custom: '', coupon_instructions: '', custom_policy_enabled: false, custom_policy_raw: '', custom_policy_summary: '', order_notice_enabled: true })
  selectStoreSku(null)
  resetImageDraft()
  productTab.value = 'knowledge'
}

function selectStoreSku(sku) {
  selectedStoreSkuKey.value = sku?.sku_key || ''
  skuStoreDraft.mode = sku?.mode || 'inherit'
  skuStoreDraft.list_ids = [...(sku?.list_ids || [])]
}

function toggleSkuStoreList(id) {
  skuStoreDraft.list_ids = skuStoreDraft.list_ids.includes(id)
    ? skuStoreDraft.list_ids.filter((value) => value !== id)
    : [...skuStoreDraft.list_ids, id]
}

async function saveSkuStoreRule() {
  const sku = productDraft.skus.find((item) => item.sku_key === selectedStoreSkuKey.value)
  if (!sku) return notify('请先选择商品规格', 'error')
  const updated = await call('POST', `/products/${encodeURIComponent(productDraft.item_id)}/sku-stores`, {
    sku_key: sku.sku_key, sku_name: sku.sku_name,
    mode: skuStoreDraft.mode,
    list_ids: skuStoreDraft.mode === 'custom' ? skuStoreDraft.list_ids : []
  })
  const index = productDraft.skus.findIndex((item) => item.sku_key === updated.sku_key)
  if (index >= 0) productDraft.skus[index] = updated
  selectStoreSku(updated)
  await refresh()
  const product = snapshot.products.find((item) => item.item_id === productDraft.item_id)
  if (product) selectProduct(product)
  notify(skuStoreDraft.mode === 'inherit' ? '该规格已恢复使用商品默认门店' : '该规格的专属门店已保存')
}

function addSelectedStoreList() {
  const id = Number(storeListSelection.value)
  if (id && !productDraft.store_list_ids.includes(id)) productDraft.store_list_ids.push(id)
  storeListSelection.value = ''
}

function unbindStoreList(id) {
  productDraft.store_list_ids = productDraft.store_list_ids.filter((value) => value !== id)
}

async function syncProducts() {
  syncReport.value = await call('POST', '/products/sync', {})
  await refresh()
  if (snapshot.products.length) selectProduct(snapshot.products[0])
  notify(`同步完成：更新 ${syncReport.value.synced} 个，未变化 ${syncReport.value.unchanged} 个${syncReport.value.failed.length ? `，失败 ${syncReport.value.failed.length} 个` : ''}`)
}

async function applySourceUpdate(section) {
  const labels = { knowledge: '商品知识', stores: '适用门店', first_reply: '首次回复' }
  if (!window.confirm(`允许本次闲鱼同步修改当前商品的${labels[section]}吗？旧版本会保留在本机。`)) return
  const updated = await call('POST', `/products/${encodeURIComponent(productDraft.item_id)}/apply-source-update`, { sections: [section] })
  selectProduct(updated)
  await refresh()
  notify(`已允许更新${labels[section]}`)
}

async function openProductPage(product = productDraft) {
  const itemId = String(product?.item_id || '').trim()
  if (!itemId) return notify('当前商品没有闲鱼商品ID', 'error')
  route.value = 'workspace'
  await nextTick()
  await desktop.browser({ action: 'navigate', url: `https://h5.m.goofish.com/item?id=${encodeURIComponent(itemId)}` })
}

async function confirmOpenProductPage(product = productDraft) {
  if (!window.confirm(`是否打开“${product?.title || '当前商品'}”的闲鱼商品页面？`)) return
  await openProductPage(product)
}

async function deleteProduct(product) {
  const title = product?.title || product?.item_id || '当前商品'
  const online = product?.item_status !== 'offline'
  const message = online
    ? `确定删除“${title}”的本地资料吗？\n\n该商品仍在闲鱼在售。点击“确定”后会同时加入忽略列表，今后同步不会重新导入。闲鱼平台商品不会被删除。`
    : `确定删除“${title}”的本地资料吗？闲鱼平台商品和历史退款记录不会被删除。`
  if (!window.confirm(message)) return
  await call('DELETE', `/products/${encodeURIComponent(product.item_id)}?ignore_on_sync=${online ? 1 : 0}`)
  newProduct()
  await refresh()
  if (snapshot.products.length) selectProduct(snapshot.products[0])
  notify('商品本地资料已删除')
}

async function setProductAiEnabled(product, enabled = !Boolean(product.enabled)) {
  const updated = await call('POST', `/products/${encodeURIComponent(product.item_id)}/enabled`, { enabled })
  await refresh()
  if (productDraft.item_id === product.item_id) selectProduct(updated)
  notify(enabled ? '当前商品AI客服已开启' : '当前商品AI客服已关闭')
}

async function openWorkspaceUrl(url, missingMessage) {
  if (!url) return notify(missingMessage, 'error')
  route.value = 'workspace'
  await nextTick()
  await desktop.browser({ action: 'navigate', url })
}

async function openReviewConversation(review) {
  await openWorkspaceUrl(review.conversation_url, '该审核记录尚未获取到对应会话链接')
}

async function openReviewOrder(review) {
  await openWorkspaceUrl(review.order_url, '该审核记录尚未获取到对应订单链接')
}

async function deleteStoreList(list) {
  const count = Array.isArray(list.item_ids) ? list.item_ids.length : 0
  const impact = count ? `，当前被 ${count} 个商品使用` : ''
  if (!window.confirm(`确定删除门店表“${list.name}”吗${impact}？删除后无法恢复。`)) return
  await call('DELETE', `/store-lists/${list.id}`)
  productDraft.store_list_ids = productDraft.store_list_ids.filter((id) => id !== list.id)
  await refresh()
  const updated = snapshot.products.find((item) => item.item_id === productDraft.item_id)
  if (updated) selectProduct(updated)
  notify('门店表已删除')
}

async function saveProduct() {
  if (!productDraft.item_id.trim()) return notify('请填写闲鱼商品ID', 'error')
  const product = await call('POST', '/products', {
    item_id: productDraft.item_id,
    title: productDraft.title,
    raw_text: productDraft.raw_text,
    enabled: productDraft.enabled,
    first_reply_enabled: productDraft.first_reply_enabled,
    first_reply_text: productDraft.first_reply_text,
    first_reply_manual: productTab.value === 'firstReply' || productDraft.first_reply_manual,
    coupon_type: productDraft.coupon_type,
    coupon_type_custom: productDraft.coupon_type_custom,
    coupon_instructions: productDraft.coupon_instructions,
    custom_policy_enabled: productDraft.custom_policy_enabled,
    custom_policy_raw: productDraft.custom_policy_raw,
    custom_policy_summary: productDraft.custom_policy_summary,
    order_notice_enabled: productDraft.order_notice_enabled
  })
  selectProduct(product)
  await refresh()
  notify('原始资料已保存，并创建版本记录')
}

async function summarizeProduct() {
  await saveProduct()
  const product = await call('POST', `/products/${encodeURIComponent(productDraft.item_id)}/summarize`, {})
  selectProduct(product)
  await refresh()
  notify('AI归纳完成，请核对高风险字段')
}

async function adoptEditedAiSummary() {
  const text = String(productDraft.ai_summary || '').trim()
  if (!text) return notify('当前没有可保存的归纳知识', 'error')
  productDraft.raw_text = text
  await saveProduct()
  notify('修改后的归纳知识已保存为当前最高优先级知识')
}

async function loadVersions() {
  if (!productDraft.item_id) return
  versions.value = await call('GET', `/products/${encodeURIComponent(productDraft.item_id)}/versions`)
  showVersions.value = true
}

async function restoreVersion(version) {
  if (!window.confirm(`确定启用版本 #${version.id}（${version.note || '历史版本'}）吗？\n当前内容会自动保存为新的版本，可再次回退。`)) return
  const updated = await call('POST', `/products/${encodeURIComponent(productDraft.item_id)}/versions/${version.id}/restore`, {})
  selectProduct(updated)
  versions.value = await call('GET', `/products/${encodeURIComponent(productDraft.item_id)}/versions`)
  await refresh()
  notify(`已启用历史版本 #${version.id}`)
}

async function chooseExcel() {
  const file = await desktop.chooseExcel()
  if (file) {
    importDraft.path = file
    if (!importDraft.name) importDraft.name = file.split(/[\\/]/).pop().replace(/\.[^.]+$/, '')
  }
}

async function regenerateFirstReply() {
  if (!productDraft.item_id) return notify('请先选择商品', 'error')
  const product = await call('POST', `/products/${encodeURIComponent(productDraft.item_id)}/first-reply/regenerate`, {})
  selectProduct(product)
  await refresh()
  notify('首次回复已根据当前商品知识重新提取')
}

async function previewStoreText() {
  if (!importDraft.text.trim()) return notify('请先粘贴门店文本', 'error')
  importDraft.preview = await call('POST', '/store-lists/preview-text', { text: importDraft.text })
  notify(`已归纳 ${importDraft.preview.store_count} 家门店`)
}

async function importStoreText() {
  if (!importDraft.text.trim()) return notify('请先粘贴门店文本', 'error')
  if (!productDraft.item_id) return notify('请先选择商品', 'error')
  const result = await call('POST', '/store-lists/import-text', {
    text: importDraft.text,
    name: importDraft.name,
    item_ids: [productDraft.item_id],
    replace_item_bindings: true
  })
  importDraft.text = ''
  importDraft.preview = null
  importDraft.name = ''
  await refresh()
  const product = snapshot.products.find((item) => item.item_id === productDraft.item_id)
  if (product) selectProduct(product)
  notify(`成功导入并绑定 ${result.store_count} 家门店`)
}

async function importStores() {
  if (!importDraft.path) return notify('请先选择Excel、CSV或TXT门店文件', 'error')
  if (!productDraft.item_id) return notify('请先选择商品', 'error')
  const result = await call('POST', '/store-lists/import', {
    path: importDraft.path,
    name: importDraft.name,
    item_ids: [productDraft.item_id],
    replace_item_bindings: true
  })
  importDraft.path = ''
  importDraft.name = ''
  await refresh()
  notify(`成功导入 ${result.store_count} 家门店`)
}

async function rebindStoreList(list) {
  await call('POST', `/store-lists/${list.id}/bind`, { item_ids: list.item_ids })
  await refresh()
  notify('商品绑定已更新')
}

async function saveProductStoreBindings() {
  if (!productDraft.item_id) return notify('请先选择商品', 'error')
  const product = await call('POST', `/products/${encodeURIComponent(productDraft.item_id)}/stores`, { list_ids: productDraft.store_list_ids })
  selectProduct(product)
  await refresh()
  notify('当前商品的适用门店已更新')
}

async function savePolicies() {
  await call('POST', '/policies', {
    reply_mode: policyDraft.reply_mode,
    global_system_prompt: policyDraft.global_system_prompt,
    max_reply_rounds: Number(policyDraft.max_reply_rounds),
    conversation_reset_hours: Number(policyDraft.conversation_reset_hours),
    safe_fallback: policyDraft.safe_fallback,
    manual_review_notice: policyDraft.manual_review_notice,
    price_fallback: policyDraft.price_fallback,
    refund_fallback: policyDraft.refund_fallback,
    order_payment_notice_enabled: policyDraft.order_payment_notice_enabled,
    aftersale_policy_raw: policyDraft.aftersale_policy_raw,
    aftersale_policy_summary: policyDraft.aftersale_policy_summary,
    forbidden_phrases: policyDraft.forbidden_phrases_text.split('\n').map((value) => value.trim()).filter(Boolean)
  })
  await refresh()
  notify('客服约束已保存')
}

async function summarizeDefaultPolicy() {
  if (!policyDraft.aftersale_policy_raw.trim()) return notify('请先填写默认发货与退款政策', 'error')
  const result = await call('POST', '/aftersale-policy/summarize', { text: policyDraft.aftersale_policy_raw })
  policyDraft.aftersale_policy_summary = result.summary
  notify('默认政策已由AI归纳，请核对后保存')
}

async function saveAfterSalePolicy() {
  await call('POST', '/policies', {
    order_payment_notice_enabled: policyDraft.order_payment_notice_enabled,
    aftersale_policy_raw: policyDraft.aftersale_policy_raw,
    aftersale_policy_summary: policyDraft.aftersale_policy_summary
  })
  await refresh()
  notify('默认发货与退款政策已保存')
}

function selectAftersaleProduct(itemId) {
  aftersaleProductId.value = itemId
  const product = snapshot.products.find((item) => item.item_id === itemId)
  if (product) selectProduct(product)
}

async function summarizeCustomPolicy() {
  if (!productDraft.item_id) return notify('请先选择商品', 'error')
  if (!productDraft.custom_policy_raw.trim()) return notify('请先填写商品特殊政策', 'error')
  const result = await call('POST', '/aftersale-policy/summarize', { text: productDraft.custom_policy_raw })
  productDraft.custom_policy_summary = result.summary
  notify('商品特殊政策已由AI归纳，请核对后保存')
}

async function saveCustomPolicy() {
  if (!productDraft.item_id) return notify('请先选择商品', 'error')
  await saveProduct()
  notify('当前商品特殊政策已保存')
}

async function runTest() {
  testResult.value = await call('POST', '/test', {
    item_id: testDraft.item_id,
    message: testDraft.message,
    store_query: testDraft.store_query,
    at: testDraft.at
  })
  notify('测试完成，消息未发送给买家')
}

async function handleReview(review, action) {
  const reply = review.final_reply || review.draft_reply
  await call('POST', `/reviews/${review.id}/${action}`, { reply })
  await refresh()
  notify(action === 'approve' ? '回复已批准发送' : '回复已拒绝')
}

async function getConfig() {
  const config = await desktop.getConfig()
  Object.assign(configDraft, config, { api_key: '' })
}

async function saveConfig() {
  await desktop.saveConfig({ api_key: configDraft.api_key, base_url: configDraft.base_url, model: configDraft.model })
  configDraft.api_key = ''
  await getConfig()
  notify('AI配置已使用 Windows 加密保存')
}

function resetImageDraft() {
  Object.assign(imageDraft, { id: null, source_path: '', name: '', purpose: '', trigger_words_text: '', reply_text: '', enabled: true })
}

async function loadImagePreviews(assets) {
  for (const asset of assets) {
    if (imagePreviews[asset.id]) continue
    try {
      imagePreviews[asset.id] = await desktop.imagePreview(asset.file_path)
    } catch (_error) {
      imagePreviews[asset.id] = ''
    }
  }
}

async function choosePackageImage() {
  const file = await desktop.chooseImage()
  if (!file) return
  imageDraft.source_path = file
  if (!imageDraft.name) imageDraft.name = file.split(/[\\/]/).pop().replace(/\.[^.]+$/, '')
}

function editImageAsset(asset) {
  Object.assign(imageDraft, {
    id: asset.id,
    source_path: asset.file_path,
    name: asset.name,
    purpose: asset.purpose || '',
    trigger_words_text: (asset.trigger_words || []).join('\n'),
    reply_text: asset.reply_text || '',
    enabled: Boolean(asset.enabled)
  })
}

async function saveImageAsset() {
  if (!productDraft.item_id) return notify('请先选择商品', 'error')
  if (!imageDraft.source_path) return notify('请先选择套餐图片', 'error')
  const asset = await call('POST', `/products/${encodeURIComponent(productDraft.item_id)}/images`, {
    id: imageDraft.id,
    source_path: imageDraft.source_path,
    name: imageDraft.name,
    purpose: imageDraft.purpose,
    trigger_words: imageDraft.trigger_words_text,
    reply_text: imageDraft.reply_text,
    enabled: imageDraft.enabled
  })
  resetImageDraft()
  await refresh()
  const product = snapshot.products.find((item) => item.item_id === productDraft.item_id)
  if (product) selectProduct(product)
  notify(`套餐图片“${asset.name}”已保存`)
}

async function deleteImageAsset(asset) {
  await call('DELETE', `/images/${asset.id}`)
  delete imagePreviews[asset.id]
  await refresh()
  const product = snapshot.products.find((item) => item.item_id === productDraft.item_id)
  if (product) selectProduct(product)
  notify('套餐图片已删除')
}

async function resetConversation(conversation) {
  await call('POST', `/conversations/${encodeURIComponent(conversation.scope_id)}/reset`, {})
  await refresh()
  notify('该会话已清零并恢复自动回复')
}

async function testAi() {
  if (configDraft.api_key) await saveConfig()
  const result = await call('POST', '/config/test-ai', {})
  notify(result.reply || '连接成功')
}

async function syncCookie() {
  const result = await desktop.syncCookie()
  await getConfig()
  notify(result.saved ? `已同步 ${result.count} 个闲鱼登录凭据` : '尚未检测到闲鱼登录，请先登录', result.saved ? 'ok' : 'error')
}

async function toggleService() {
  const endpoint = serviceActive.value ? '/service/stop' : '/service/start'
  await call('POST', endpoint, {})
  await refresh()
}

function changeRoute(nextRoute) {
  route.value = nextRoute
  nextTick(syncBrowserBounds)
}

function syncBrowserBounds() {
  if (route.value !== 'workspace' || !browserStage.value) {
    desktop.setBrowserBounds({ x: 0, y: 0, width: 0, height: 0 })
    return
  }
  const rect = browserStage.value.getBoundingClientRect()
  desktop.setBrowserBounds({ x: rect.x, y: rect.y, width: rect.width, height: rect.height })
}

watch([route, aiPanelOpen, sidebarCollapsed], () => nextTick(syncBrowserBounds))

onMounted(async () => {
  const [, , identity] = await Promise.all([refresh(false), getConfig(), desktop.getVersion()])
  Object.assign(appIdentity, identity || {})
  resizeObserver = new ResizeObserver(syncBrowserBounds)
  resizeObserver.observe(document.body)
  if (browserStage.value) resizeObserver.observe(browserStage.value)
  removeEventListener = desktop.onAppEvent((event) => {
    if (event.type === 'cookie-synced') {
      configDraft.cookie_saved = true
      configDraft.cookie_updated_at = event.at
    }
    if (event.type === 'backend-exit') notify('本地AI服务意外停止', 'error')
  })
  refreshTimer = setInterval(() => refresh(true).catch(() => {}), 2500)
  nextTick(syncBrowserBounds)
})

onBeforeUnmount(() => {
  clearInterval(refreshTimer)
  resizeObserver?.disconnect()
  removeEventListener?.()
  desktop.setBrowserBounds({ x: 0, y: 0, width: 0, height: 0 })
})
</script>

<template>
  <div class="app-shell" :class="{ 'sidebar-mini': sidebarCollapsed }">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">券</div>
        <div v-if="!sidebarCollapsed" class="brand-copy"><strong>闲鱼卡券</strong><span>AI 客服 {{ appIdentity.edition }} · {{ appIdentity.backend_version || appIdentity.frontend_version }}</span></div>
      </div>
      <nav class="nav-list">
        <button v-for="item in navItems" :key="item.id" :class="['nav-item', { active: route === item.id }]" @click="changeRoute(item.id)">
          <span class="nav-icon">{{ item.icon }}</span><span v-if="!sidebarCollapsed" class="nav-label">{{ item.label }}</span>
          <span v-if="!sidebarCollapsed && item.badge && item.badge()" class="nav-badge">{{ item.badge() }}</span>
        </button>
      </nav>
      <div class="sidebar-bottom">
        <div class="connection"><i :class="snapshot.service.status"></i><span v-if="!sidebarCollapsed">{{ serviceLabel }}</span></div>
        <button class="collapse-button" @click="sidebarCollapsed = !sidebarCollapsed">{{ sidebarCollapsed ? '›' : '‹ 收起导航' }}</button>
      </div>
    </aside>

    <main class="main-area">
      <header class="topbar">
        <div><span class="eyebrow">餐饮电子券智能客服</span><h1>{{ pageTitle }}</h1></div>
        <div class="top-actions">
          <div class="time-chip">北京时间 · 自动识别工作日/周末/餐段</div>
          <button :class="['service-button', { stop: serviceActive }]" @click="toggleService">{{ serviceActive ? '停止客服' : '启动客服' }}</button>
        </div>
      </header>

      <section v-if="route === 'dashboard'" class="page scroll-page">
        <div class="hero-card">
          <div><span class="eyebrow">今日工作台</span><h2>客服状态一眼看清</h2><p>商品原始资料优先，门店查询与当前时间由本地引擎裁决。</p></div>
          <button class="primary" @click="changeRoute('workspace')">进入闲鱼工作台</button>
        </div>
        <div class="metric-grid">
          <article class="metric-card"><span>已配置商品</span><strong>{{ snapshot.dashboard.products }}</strong><small>{{ snapshot.dashboard.enabled_products }} 个正在启用</small></article>
          <article class="metric-card"><span>可用门店</span><strong>{{ snapshot.dashboard.stores }}</strong><small>{{ snapshot.dashboard.store_lists }} 份门店表</small></article>
          <article class="metric-card warning"><span>待审核回复</span><strong>{{ snapshot.dashboard.audits.pending || 0 }}</strong><small>高风险内容不会自动发送</small></article>
          <article class="metric-card"><span>已发送回复</span><strong>{{ snapshot.dashboard.audits.sent || 0 }}</strong><small>本机留存审计记录</small></article>
        </div>
        <div class="two-column">
          <article class="panel"><div class="panel-head"><div><span class="eyebrow">运行原则</span><h3>三层事实优先级</h3></div></div><ol class="priority-list"><li><b>1</b><div><strong>你提供的原始文本</strong><span>永久最高优先级，不会被AI覆盖</span></div></li><li><b>2</b><div><strong>AI归纳摘要</strong><span>方便阅读，关键字段需核对</span></div></li><li><b>3</b><div><strong>闲鱼商品页面</strong><span>只补充前两层没有的信息</span></div></li></ol></article>
          <article class="panel"><div class="panel-head"><div><span class="eyebrow">最近动态</span><h3>本机运行记录</h3></div></div><div class="event-list"><div v-for="event in snapshot.dashboard.events" :key="event.id"><i></i><span>{{ event.message }}</span><time>{{ event.created_at }}</time></div><p v-if="!snapshot.dashboard.events.length" class="empty">还没有运行记录</p></div></article>
        </div>
      </section>

      <section v-else-if="route === 'workspace'" class="workspace-page">
        <div class="browser-column">
          <div class="browser-toolbar">
            <button @click="desktop.browser('back')">←</button><button @click="desktop.browser('forward')">→</button><button @click="desktop.browser('reload')">↻</button>
            <div class="address"><span>🔒</span> www.goofish.com/im</div>
            <button @click="desktop.browser('home')">回到消息</button><button @click="syncCookie">同步登录</button>
          </div>
          <div ref="browserStage" class="browser-stage"><div class="browser-placeholder"><strong>正在载入完整闲鱼网页</strong><span>登录状态会保存在本机，不需要手动复制 Cookie</span></div></div>
        </div>
        <aside v-if="aiPanelOpen" class="ai-panel">
          <div class="ai-panel-head"><div><span class="live-dot"></span><strong>AI 回复助手</strong><small>仅当前商品</small></div><button @click="aiPanelOpen = false">›</button></div>
          <div class="ai-scroll">
            <div class="context-card"><span class="mini-label">当前商品</span><strong>{{ currentProduct?.title || '尚未选择商品' }}</strong><small>ID {{ currentProduct?.item_id || '—' }}</small></div>
            <div class="rule-strip"><span>知识优先级</span><b>原文 ＞ AI摘要 ＞ 闲鱼页面</b></div>
            <div class="assistant-block"><div class="block-title"><span>买家连续消息</span><em>约0.2秒极速聚合</em></div><div class="message-bubble">等待闲鱼新消息…</div><small>固定规则会本地直返；新消息到达会自动废弃旧草稿，防止答非所问。</small></div>
            <div class="assistant-block"><div class="block-title"><span>回复草稿</span><em class="safe">安全检查</em></div><textarea placeholder="收到消息后在此显示草稿；高风险内容必须人工确认"></textarea><div class="draft-actions"><button>转人工</button><button class="primary" disabled>确认发送</button></div></div>
            <div class="evidence-card"><strong>回答依据</strong><div><i class="gold"></i>用户原始资料</div><div><i></i>当前商品绑定门店表</div><div><i></i>北京时间与当前商品知识</div></div>
          </div>
        </aside>
        <button v-else class="open-ai-panel" @click="aiPanelOpen = true">‹ AI</button>
      </section>

      <section v-else-if="route === 'aftersale'" class="page scroll-page narrow-page">
        <div class="page-heading">
          <div><span class="eyebrow">自然语言配置</span><h2>发货与退款政策</h2><p>先设置全店默认政策；特殊商品可单独覆盖。AI只负责归纳，最终以你保存的文本为准。</p></div>
          <button class="primary" @click="saveAfterSalePolicy">保存默认政策</button>
        </div>
        <article class="panel form-panel policy-editor">
          <div class="knowledge-head"><div><span class="number">默</span><div><strong>全店默认发货与退款政策</strong><small>用于退款、有效期、当天使用、自动发货等售后问题</small></div></div><span class="authority">默认生效</span></div>
          <label class="check-line"><input v-model="policyDraft.order_payment_notice_enabled" type="checkbox" />买家拍下未付款时，自动发送“商品信息＋使用规则＋发货与退换货政策”</label>
          <label>你的原始政策描述<textarea v-model="policyDraft.aftersale_policy_raw" rows="8" placeholder="直接用自然语言填写政策，不需要整理成固定模板。"></textarea><small>原文永久保留，不会被AI覆盖。</small></label>
          <div class="button-row end"><button @click="summarizeDefaultPolicy">✦ 让AI归纳</button></div>
          <label>当前生效的政策摘要<textarea v-model="policyDraft.aftersale_policy_summary" rows="10" placeholder="AI归纳后可继续人工修改；客服按这里回答。"></textarea></label>
        </article>

        <div class="page-heading sub"><div><span class="eyebrow">商品级覆盖</span><h2>特殊商品政策</h2><p>未启用时自动使用上面的全店默认政策。</p></div></div>
        <article class="panel form-panel policy-editor">
          <label>选择商品<select v-model="aftersaleProductId" @change="selectAftersaleProduct(aftersaleProductId)"><option value="">请选择商品</option><option v-for="product in snapshot.products" :key="product.item_id" :value="product.item_id">{{ product.title || product.item_id }}</option></select></label>
          <template v-if="productDraft.item_id">
            <label class="check-line"><input v-model="productDraft.order_notice_enabled" type="checkbox" />当前商品启用“拍下未付款”自动提醒</label>
            <label class="check-line"><input v-model="productDraft.custom_policy_enabled" type="checkbox" />当前商品使用独立的发货与退款政策</label>
            <template v-if="productDraft.custom_policy_enabled">
              <label>当前商品特殊政策原文<textarea v-model="productDraft.custom_policy_raw" rows="7" placeholder="只填写与默认政策不同或需要特别说明的内容。"></textarea></label>
              <div class="button-row end"><button @click="summarizeCustomPolicy">✦ 让AI归纳商品政策</button></div>
              <label>当前商品生效摘要<textarea v-model="productDraft.custom_policy_summary" rows="8"></textarea></label>
            </template>
            <div v-else class="security-note">当前商品沿用全店默认政策。</div>
            <div class="button-row end"><button class="primary" @click="saveCustomPolicy">保存当前商品政策</button></div>
          </template>
        </article>
      </section>

      <section v-else-if="route === 'products'" class="page split-page">
        <aside class="list-panel">
          <div class="list-head"><div><span class="eyebrow">在售商品</span><h3>{{ snapshot.products.length }} 个商品</h3></div><button class="square primary" @click="newProduct">＋</button></div>
          <button class="sync-products" @click="syncProducts">↻ 同步闲鱼在售商品</button>
          <input v-model="productSearch" class="search-input" placeholder="搜索商品名称或ID" />
          <div v-for="product in filteredProducts" :key="product.item_id" :class="['product-row', { active: productDraft.item_id === product.item_id }]" @click="openProductKnowledge(product)">
            <span class="product-thumb image"><img v-if="product.thumbnail_url" :src="product.thumbnail_url" /><b v-else>券</b></span>
            <span><strong>{{ product.title || '未命名商品' }}</strong><small>¥{{ product.price || '—' }} · {{ product.store_lists?.reduce((sum, item) => sum + item.store_count, 0) || 0 }} 家门店</small><em>{{ product.item_status === 'offline' ? '已下架' : product.sync_status === 'source_updated' ? '闲鱼来源有更新' : !product.coupon_type ? '卡券类型待补充' : '知识已就绪' }}</em></span>
            <span class="product-row-actions"><button class="ai-toggle" :class="{ off: !product.enabled }" :title="product.enabled ? '点击关闭当前商品AI客服' : '点击开启当前商品AI客服'" @click.stop="setProductAiEnabled(product)">{{ product.enabled ? 'AI开' : 'AI关' }}</button><button title="打开商品页面" @click.stop="confirmOpenProductPage(product)">↗</button><button class="danger-icon" title="删除本地商品" @click.stop="deleteProduct(product)">删</button></span>
          </div>
          <div v-if="!filteredProducts.length" class="empty-card">登录闲鱼后点击“同步闲鱼在售商品”。</div>
        </aside>
        <div class="editor-panel scroll-panel">
          <div class="product-hero">
            <div class="product-cover"><img v-if="productDraft.thumbnail_url" :src="productDraft.thumbnail_url" /><span v-else>券</span></div>
            <div><span class="eyebrow">商品卡片</span><h2>{{ productDraft.title || '新建商品' }}</h2><p>ID {{ productDraft.item_id || '—' }} · ¥{{ productDraft.price || '—' }} · {{ productDraft.item_status === 'offline' ? '已下架' : '在售' }}</p></div>
            <div class="button-row"><button :class="productDraft.enabled ? '' : 'danger'" @click="setProductAiEnabled(productDraft)">{{ productDraft.enabled ? '关闭本商品AI客服' : '开启本商品AI客服' }}</button><button @click="confirmOpenProductPage()">打开商品页面</button><button @click="loadVersions">版本记录</button><button class="danger" @click="deleteProduct(productDraft)">删除</button><button class="primary" @click="saveProduct">保存人工修改</button></div>
          </div>
          <div class="product-tabs"><button :class="{ active: productTab === 'knowledge' }" @click="productTab = 'knowledge'">商品资料与知识</button><button :class="{ active: productTab === 'firstReply' }" @click="productTab = 'firstReply'">首次回复</button><button :class="{ active: productTab === 'stores' }" @click="productTab = 'stores'">适用门店 <b>{{ productDraft.store_lists.reduce((sum, item) => sum + item.store_count, 0) }}</b></button><button :class="{ active: productTab === 'images' }" @click="productTab = 'images'">套餐图片 <b>{{ productDraft.image_assets.length }}</b></button></div>

          <article v-if="productDraft.sync_status === 'source_updated'" class="knowledge-card source-update-card">
            <div class="knowledge-head"><div><span class="number">新</span><div><strong>闲鱼页面发现更新</strong><small>当前生效知识、门店和首次回复均未被覆盖；只有你确认的项目才会修改。</small></div></div><span class="authority">等待人工允许</span></div>
            <div class="button-row end">
              <button :disabled="productDraft.source_update?.resolved_sections?.includes('knowledge')" @click="applySourceUpdate('knowledge')">允许更新商品知识</button>
              <button :disabled="productDraft.source_update?.resolved_sections?.includes('stores')" @click="applySourceUpdate('stores')">允许更新适用门店</button>
              <button :disabled="productDraft.source_update?.resolved_sections?.includes('first_reply')" class="primary" @click="applySourceUpdate('first_reply')">允许更新首次回复</button>
            </div>
          </article>

          <div v-if="productTab === 'knowledge'" class="product-tab-body">
            <div v-if="productDraft.source_type === 'manual' || !productDraft.item_id" class="manual-product-fields">
              <label>闲鱼商品ID<input v-model="productDraft.item_id" :readonly="snapshot.products.some((item) => item.item_id === productDraft.item_id)" placeholder="粘贴商品链接中的 itemId" /></label>
              <label>商品名称<input v-model="productDraft.title" placeholder="例如：小江溪125元代金券" /></label>
            </div>
            <article class="knowledge-card coupon-settings"><div class="knowledge-head"><div><span class="number">券</span><div><strong>卡券类型与领取方式</strong><small>新商品默认使用美团卡券，也可在此为特殊商品单独修改</small></div></div><span class="authority">人工确定</span></div><div class="form-grid two"><label>卡券类型<select v-model="productDraft.coupon_type"><option value="meituan">美团卡券（默认）</option><option value="douyin">抖音卡券</option><option value="merchant_miniapp">商家小程序券</option><option value="electronic_code">普通电子券码</option><option value="purchase_order">代买单</option><option value="other">其他卡券</option></select></label><label v-if="productDraft.coupon_type === 'other'">其他卡券名称<input v-model="productDraft.coupon_type_custom" placeholder="例如：支付宝卡券" /></label><label class="wide">领取、查看与核销说明<textarea v-model="productDraft.coupon_instructions" rows="3" placeholder="留空使用当前卡券类型的默认说明；也可填写你的真实发券与核销流程。"></textarea></label></div></article>
            <div class="source-panel">
              <div class="knowledge-head"><div><span class="number">1</span><div><strong>闲鱼页面原始资料</strong><small>自动读取商品文案；图片只作界面预览，不会交给AI识别 · 最后同步 {{ productDraft.last_synced_at || '尚未同步' }}</small></div></div><span class="authority secondary">自动来源</span></div>
              <div class="source-images"><img v-for="url in productDraft.image_urls.slice(0, 8)" :key="url" :src="url" /></div>
              <pre>{{ productDraft.platform_summary || '当前没有同步到闲鱼页面资料。' }}</pre>
            </div>
            <article class="knowledge-card raw"><div class="knowledge-head"><div><span class="number">2</span><div><strong>当前生效的商品知识</strong><small>AI已根据页面生成初稿，你可以直接改写；保存后永久优先于闲鱼页面</small></div></div><span class="authority">最高优先级</span></div><textarea v-model="productDraft.raw_text" rows="14" placeholder="同步后会自动生成初始知识；也可以直接粘贴自己的补充说明。"></textarea></article>
            <article class="knowledge-card ai"><div class="knowledge-head"><div><span class="number">3</span><div><strong>最近一次AI初始知识</strong><small>可以直接修改；点击保存后成为当前最高优先级知识，并自动保留历史版本</small></div></div><button class="primary soft" @click="summarizeProduct">✦ 根据当前知识重新归纳</button></div><textarea v-if="productDraft.ai_summary" v-model="productDraft.ai_summary" rows="14" placeholder="可在这里修改AI归纳结果"></textarea><div v-else class="empty-summary">同步商品后自动生成。</div><div v-if="productDraft.ai_summary" class="button-row end"><button class="primary" @click="adoptEditedAiSummary">保存修改并设为当前知识</button></div><div v-if="productDraft.structured?.risk_fields?.length" class="risk-box"><strong>需要人工核对</strong><span v-for="field in productDraft.structured.risk_fields" :key="field">{{ field }}</span></div></article>
          </div>

          <div v-else-if="productTab === 'firstReply'" class="product-tab-body">
            <article class="knowledge-card raw first-reply-card"><div class="knowledge-head"><div><span class="number">首</span><div><strong>当前商品首次回复</strong><small>每位买家咨询当前商品时每个会话窗口只触发一次；{{ policyDraft.conversation_reset_hours }}小时无消息后重新计算</small></div></div><span class="authority">本地极速发送</span></div><label class="check-line"><input v-model="productDraft.first_reply_enabled" type="checkbox" />启用当前商品首次回复</label><textarea v-model="productDraft.first_reply_text" rows="12" placeholder="系统会把每个面额、售价、适用时间和发券组成分别渲染；不会发送JSON或字段对象。"></textarea><div class="first-reply-preview"><strong>实际发送预览</strong><pre>{{ productDraft.first_reply_text || '尚未生成首次回复。' }}</pre></div><div class="first-reply-meta"><span>生成时间：{{ productDraft.first_reply_generated_at || '尚未生成' }}</span><span>预览内容与实际发送文本一致</span></div><div class="button-row end"><button @click="regenerateFirstReply">重新从当前知识提取</button><button class="primary" @click="saveProduct">保存首次回复</button></div></article>
          </div>

          <div v-else-if="productTab === 'stores'" class="product-tab-body">
            <article class="import-card compact"><div class="upload-icon">表</div><div class="upload-copy"><strong>{{ importDraft.path ? importDraft.path.split(/[\\/]/).pop() : '导入Excel、CSV或TXT门店文件' }}</strong><span>字段可以不完整；识别店名、分店名、省、市、区县、地址、电话并自动去重。</span></div><button @click="chooseExcel">选择文件</button><label>门店表名称<input v-model="importDraft.name" placeholder="例如：小江溪全国门店" /></label><button class="primary" @click="importStores">导入并替换当前商品门店</button></article>
            <article class="knowledge-card store-text-import"><div class="knowledge-head"><div><span class="number">文</span><div><strong>粘贴TXT门店文本</strong><small>支持【省份】【城市】门店1、门店2；先由AI归纳预览，再绑定当前商品</small></div></div></div><label>门店表名称<input v-model="importDraft.name" placeholder="例如：全国可用门店" /></label><textarea v-model="importDraft.text" rows="9" placeholder="【湖北省】&#10;【武汉】武汉新荣天街店、武昌万象城店&#10;&#10;【四川省】&#10;【成都】成都IFS店、成都万象城店"></textarea><div class="button-row end"><button @click="previewStoreText">AI归纳预览</button><button class="primary" @click="importStoreText">导入并绑定当前商品</button></div><div v-if="importDraft.preview" class="store-text-preview"><strong>识别到 {{ importDraft.preview.province_count }} 个省份、{{ importDraft.preview.city_count }} 个城市、{{ importDraft.preview.store_count }} 家门店</strong><p class="ai-store-summary">AI归纳：{{ importDraft.preview.ai_summary }}</p><div v-for="group in importDraft.preview.preview" :key="`${group.province}-${group.city}`"><b>{{ group.province }} · {{ group.city }}</b><span>{{ group.stores.join('、') }}</span></div><p v-for="warning in importDraft.preview.warnings" :key="warning">⚠ {{ warning }}</p></div></article>
            <article class="knowledge-card compact-store-manager"><div class="knowledge-head"><div><span class="number">店</span><div><strong>当前商品绑定的门店表</strong><small>从下拉菜单添加；点击已绑定标签的 × 可单独解除</small></div></div><button class="primary soft" @click="saveProductStoreBindings">保存门店绑定</button></div><div class="store-select-row"><select v-model="storeListSelection" @change="addSelectedStoreList"><option value="">＋ 选择适用门店表</option><option v-for="list in snapshot.store_lists.filter((item) => !productDraft.store_list_ids.includes(item.id))" :key="list.id" :value="list.id">{{ list.name }}（{{ list.store_count }}家）</option></select><details class="store-delete-menu"><summary>管理/删除门店表⌄</summary><div class="store-delete-popover"><div v-for="list in snapshot.store_lists" :key="list.id"><span><strong>{{ list.name }}</strong><small>{{ list.store_count }}家 · 已绑定{{ list.item_ids?.length || 0 }}个商品</small></span><button class="danger" @click="deleteStoreList(list)">删除</button></div><p v-if="!snapshot.store_lists.length">暂无门店表</p></div></details></div><div class="bound-store-chips"><span v-for="list in snapshot.store_lists.filter((item) => productDraft.store_list_ids.includes(item.id))" :key="list.id">{{ list.name }}（{{ list.store_count }}家）<button title="解除当前商品绑定" @click="unbindStoreList(list.id)">×</button></span><p v-if="!productDraft.store_list_ids.length">当前商品尚未绑定门店表。</p></div></article>
            <article class="knowledge-card sku-store-card">
              <div class="knowledge-head"><div><span class="number">规</span><div><strong>不同规格适用门店</strong><small>默认继承上面的商品门店；仅在特殊规格门店不同时单独设置</small></div></div><button class="primary soft" :disabled="!selectedStoreSkuKey" @click="saveSkuStoreRule">保存规格门店</button></div>
              <div v-if="productDraft.skus.length" class="sku-store-layout">
                <div class="sku-store-options">
                  <button v-for="sku in productDraft.skus" :key="sku.sku_key" :class="['sku-store-option', { active: selectedStoreSkuKey === sku.sku_key }]" @click="selectStoreSku(sku)">
                    <strong>{{ sku.sku_name }}</strong><small>{{ sku.sale_price ? `售价${sku.sale_price}元 · ` : '' }}{{ sku.mode === 'custom' ? `专属门店${sku.effective_store_count}家` : `继承默认门店${sku.effective_store_count}家` }}</small>
                  </button>
                </div>
                <div class="sku-store-editor">
                  <label class="radio-line"><input v-model="skuStoreDraft.mode" type="radio" value="inherit" />继承商品默认门店 <small>默认选项，商品门店更新后自动同步</small></label>
                  <label class="radio-line"><input v-model="skuStoreDraft.mode" type="radio" value="custom" />使用该规格专属门店 <small>只影响当前选中的规格</small></label>
                  <div v-if="skuStoreDraft.mode === 'custom'" class="sku-list-checks">
                    <label v-for="list in snapshot.store_lists" :key="list.id"><input type="checkbox" :checked="skuStoreDraft.list_ids.includes(list.id)" @change="toggleSkuStoreList(list.id)" />{{ list.name }}（{{ list.store_count }}家）</label>
                    <p v-if="!snapshot.store_lists.length">请先在上方导入门店表。</p>
                    <p v-else-if="!skuStoreDraft.list_ids.length" class="warning-text">尚未选择门店表。保存后客服不会猜测，会转人工核实该规格门店。</p>
                  </div>
                </div>
              </div>
              <p v-else>当前商品还没有识别到可售规格，请先完善商品知识并重新归纳。</p>
            </article>
          </div>

          <div v-else-if="productTab === 'images'" class="product-tab-body image-library">
            <article class="knowledge-card image-editor"><div class="knowledge-head"><div><span class="number">图</span><div><strong>当前商品的套餐图片</strong><small>图片仅保存在本机并在命中触发词后发给买家，不发送给AI模型</small></div></div><button v-if="imageDraft.id" @click="resetImageDraft">取消编辑</button></div>
              <div class="image-form-grid">
                <div class="image-picker"><button class="primary soft" @click="choosePackageImage">选择图片</button><span>{{ imageDraft.source_path ? imageDraft.source_path.split(/[\\/]/).pop() : '支持 PNG、JPG、JPEG、WebP，最大10MB' }}</span></div>
                <label>图片名称<input v-model="imageDraft.name" placeholder="例如：双人套餐内容图" /></label>
                <label>用途说明<input v-model="imageDraft.purpose" placeholder="例如：买家咨询套餐包含什么时发送" /></label>
                <label class="wide">触发词（每行一个）<textarea v-model="imageDraft.trigger_words_text" rows="5" placeholder="套餐图&#10;套餐有什么&#10;发一下菜单"></textarea><small>明确命中只发送当前商品这张图；同时命中多张或模糊匹配会转人工审核。</small></label>
                <label class="wide">随图片发送的文字<input v-model="imageDraft.reply_text" placeholder="例如：可以的，给您发一下这款套餐的内容图。" /></label>
                <label class="check-line"><input v-model="imageDraft.enabled" type="checkbox" />启用这张图片</label>
                <button class="primary large" @click="saveImageAsset">{{ imageDraft.id ? '保存修改' : '添加到当前商品' }}</button>
              </div>
            </article>
            <div class="asset-grid"><article v-for="asset in productDraft.image_assets" :key="asset.id" class="asset-card"><div class="asset-preview"><img v-if="imagePreviews[asset.id]" :src="imagePreviews[asset.id]" /><span v-else>图片</span></div><div class="asset-copy"><div><strong>{{ asset.name }}</strong><em :class="{ off: !asset.enabled }">{{ asset.enabled ? '已启用' : '已停用' }}</em></div><p>{{ asset.purpose || '未填写用途说明' }}</p><small>触发词：{{ asset.trigger_words.join('、') }}</small><span>{{ asset.reply_text || '使用默认随图回复' }}</span></div><div class="asset-actions"><button @click="editImageAsset(asset)">编辑</button><button class="danger" @click="deleteImageAsset(asset)">删除</button></div></article><div v-if="!productDraft.image_assets.length" class="empty-card wide">当前商品还没有套餐图片。添加后，客服只会在这个商品的会话中匹配和发送。</div></div>
          </div>

        </div>
      </section>

      <section v-else-if="route === 'reviews'" class="page scroll-page">
        <div class="page-heading"><div><span class="eyebrow">高风险保护</span><h2>回复审核</h2><p>涉及降价、退款、赔偿、延期、换码等承诺的内容不会自动发送。</p></div><span class="count-pill">{{ snapshot.dashboard.audits.pending || 0 }} 条待处理</span></div>
        <div class="review-list"><article v-for="review in snapshot.reviews" :key="review.id" :class="['review-card', review.status]" @click="openReviewConversation(review)"><div class="review-meta"><span>#{{ review.id }} · 商品 {{ review.item_id }} · 会话 {{ review.chat_id || '未获取' }} · 订单号 {{ review.order_id || '未获取' }}</span><time>{{ review.created_at }}</time><em>{{ review.status }}</em></div><div v-if="review.image_asset_id" class="review-image-badge">随回复发送套餐图片：{{ review.image_asset_name || `#${review.image_asset_id}` }}</div><div class="conversation"><div><small>买家</small><p>{{ review.user_message }}</p></div><div><small>AI草稿</small><textarea v-model="review.draft_reply" @click.stop></textarea></div></div><div class="review-reasons"><span v-for="reason in JSON.parse(review.reasons || '[]')" :key="reason">⚠ {{ reason }}</span></div><div class="review-actions" @click.stop><button @click="openReviewConversation(review)">打开对应对话</button><button @click="openReviewOrder(review)">打开订单</button><template v-if="review.status === 'pending'"><button @click="handleReview(review, 'reject')">拒绝</button><button class="primary" @click="handleReview(review, 'approve')">确认后发送</button></template></div></article><div v-if="!snapshot.reviews.length" class="empty-card wide">目前没有需要审核的回复。</div></div>
      </section>

      <section v-else-if="route === 'guardrails'" class="page scroll-page narrow-page">
        <div class="page-heading"><div><span class="eyebrow">不可绕过的规则</span><h2>客服回答约束</h2><p>此处由确定性程序检查，不交给AI自由判断。</p></div><button class="primary" @click="savePolicies">保存约束</button></div>
        <article class="panel form-panel rule-master"><label>全局最高规则提示词<textarea v-model="policyDraft.global_system_prompt" rows="8"></textarea><small>每次AI回答都会首先加载；买家消息和商品内容不能覆盖这段规则。</small></label><div class="form-grid three"><label>默认回复模式<select v-model="policyDraft.reply_mode"><option value="review">安全审核：所有回复先确认</option><option value="auto">自动回复：仅高风险转人工</option></select></label><label>单次会话最大AI回复<select v-model.number="policyDraft.max_reply_rounds"><option :value="25">25次</option><option :value="40">40次</option><option :value="55">55次</option></select></label><label>无消息多久重新计数<select v-model.number="policyDraft.conversation_reset_hours"><option :value="1">1小时</option><option :value="6">6小时</option><option :value="12">12小时</option><option :value="24">24小时</option><option :value="48">48小时</option><option :value="72">72小时</option></select></label></div><label>议价固定婉拒回复<textarea v-model="policyDraft.price_fallback" rows="2"></textarea><small>识别到砍价后直接使用这段文字，不让AI临时发挥。</small></label><label>进入人工审核时立即发给买家的提示<textarea v-model="policyDraft.manual_review_notice" rows="2"></textarea><small>同一待审核会话只发送一次，人工确认或拒绝后自动恢复客服。</small></label><label>一般风险兜底<textarea v-model="policyDraft.safe_fallback" rows="2"></textarea></label><label>退款兜底<textarea v-model="policyDraft.refund_fallback" rows="2"></textarea></label><label>禁止承诺表达（每行一条）<textarea v-model="policyDraft.forbidden_phrases_text" rows="9"></textarea></label></article>
        <div class="page-heading sub"><div><span class="eyebrow">会话计数</span><h2>当前顾客回复进度</h2><p>达到上限后静默停止自动回复并转人工；超过上方设定的无消息时间会自动清零。</p></div></div>
        <div class="conversation-state-list"><article v-for="conversation in snapshot.conversations" :key="conversation.scope_id"><div><strong>商品 {{ conversation.item_id }}</strong><span>顾客 {{ conversation.user_id }} · 会话 {{ conversation.chat_id }}</span></div><div class="reply-progress"><b>{{ conversation.ai_reply_count }}/{{ policyDraft.max_reply_rounds }}</b><span :class="conversation.state">{{ conversation.state === 'limit_reached' ? '已停止' : '自动回复中' }}</span><button @click="resetConversation(conversation)">清零并恢复</button></div></article><div v-if="!snapshot.conversations.length" class="empty-card wide">还没有产生顾客会话。</div></div>
      </section>

      <section v-else-if="route === 'testing'" class="page scroll-page">
        <div class="page-heading"><div><span class="eyebrow">不会真实发送</span><h2>客服测试中心</h2><p>先用真实问法验证门店模糊匹配、当前时间和安全规则。</p></div><span class="safe-pill">测试沙箱</span></div>
        <div class="test-layout"><article class="panel form-panel"><label>当前商品<select v-model="testDraft.item_id"><option value="">请选择</option><option v-for="product in snapshot.products" :key="product.item_id" :value="product.item_id">{{ product.title || product.item_id }}</option></select></label><label>模拟买家消息<textarea v-model="testDraft.message" rows="5" placeholder="例如：小江溪万象城店可以用吗？"></textarea></label><label>门店关键词（可选）<input v-model="testDraft.store_query" placeholder="例如：万象城" /></label><label>指定测试时间（可选）<input v-model="testDraft.at" type="datetime-local" /></label><button class="primary large" @click="runTest">运行测试</button></article><article class="result-panel"><div v-if="testResult"><div class="result-status"><span :class="testResult.action">{{ testResult.action }}</span><small>{{ testResult.reason }}</small></div><div class="result-reply">{{ testResult.reply }}</div><div v-if="testResult.image_asset" class="review-image-badge">将发送套餐图片：{{ testResult.image_asset.name }}</div><div class="time-summary" v-if="testResult.time"><span>{{ testResult.time.day_label }}</span><span>{{ testResult.time.meal_period }}</span><span>{{ testResult.time.now }}</span></div><div class="evidence-list"><strong>本次回答依据</strong><div v-for="entry in testResult.evidence" :key="entry.source"><i :class="entry.status"></i><span>{{ entry.source }}</span><em>优先级 {{ entry.priority }}</em></div></div></div><div v-else class="empty-result"><div>测</div><strong>等待测试</strong><span>结果会显示回答文本、裁决原因和使用的数据来源。</span></div></article></div>
      </section>

      <section v-else-if="route === 'settings'" class="page scroll-page narrow-page">
        <div class="page-heading"><div><span class="eyebrow">本机安全保存</span><h2>AI 与闲鱼连接</h2><p>API Key 使用 Windows DPAPI 加密；闲鱼 Cookie 从内置网页自动同步。</p></div></div>
        <article class="panel form-panel"><div class="status-grid"><div><i :class="{ good: configDraft.api_key_saved }"></i><span>API Key</span><strong>{{ configDraft.api_key_saved ? '已安全保存' : '未配置' }}</strong></div><div><i :class="{ good: configDraft.cookie_saved }"></i><span>闲鱼登录</span><strong>{{ configDraft.cookie_saved ? '已自动同步' : '等待登录' }}</strong></div></div><label>新的 API Key<input v-model="configDraft.api_key" type="password" placeholder="留空表示不修改已保存的Key" /></label><label>模型接口地址<input v-model="configDraft.base_url" /></label><label>客服与文本归纳模型<input v-model="configDraft.model" list="text-models" /><datalist id="text-models"><option value="qwen-plus"></option><option value="qwen-flash"></option><option value="qwen-max"></option></datalist><small>推荐 qwen-plus。商品知识仅根据文案归纳，商品图片和套餐图片都不会发送给AI。</small></label><div class="button-row"><button @click="saveConfig">保存配置</button><button @click="testAi">测试AI连接</button><button class="primary" @click="syncCookie">从内置闲鱼同步登录</button></div><div class="security-note">Cookie 不会显示在界面，也不会写入安装包。套餐图片仅保存在本机，触发后直接上传闲鱼发送。</div></article>
      </section>

      <section v-else-if="route === 'logs'" class="page scroll-page">
        <div class="page-heading"><div><span class="eyebrow">审计与排错</span><h2>运行记录</h2><p>商品导入、连接变化、待审核事件均保存在本机。</p></div></div><article class="panel log-panel"><div v-for="event in snapshot.dashboard.events" :key="event.id"><time>{{ event.created_at }}</time><span>{{ event.event_type }}</span><p>{{ event.message }}</p></div><div v-if="!snapshot.dashboard.events.length" class="empty">暂无记录</div></article>
      </section>
    </main>

    <div v-if="loading" class="loading-overlay"><div class="spinner"></div><span>正在处理…</span></div>
    <transition name="toast"><div v-if="toast.visible" :class="['toast', toast.kind]">{{ toast.text }}</div></transition>
    <div v-if="showVersions" class="modal-backdrop" @click.self="showVersions = false"><div class="modal"><div class="modal-head"><h3>知识库版本记录</h3><button @click="showVersions = false">×</button></div><div class="version-list"><article v-for="version in versions" :key="version.id"><strong>{{ version.note }}</strong><time>{{ version.created_at }}</time><p>{{ version.raw_text.slice(0, 220) || '空资料' }}{{ version.raw_text.length > 220 ? '…' : '' }}</p><div class="button-row end"><button class="primary" @click="restoreVersion(version)">启用此版本</button></div></article></div></div></div>
  </div>
</template>
