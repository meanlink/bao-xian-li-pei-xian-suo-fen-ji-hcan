import {get, post} from '../api.js';
import {Layout, Section, openModal, showToast} from '../components/ui.js';
import {canTriage, dateText, escapeHtml, money} from '../utils/constants.js';

const queues = {
  overdue: {label: '已逾期', badge: 'badge-red'},
  due_today: {label: '今日到期', badge: 'badge-amber'},
  unscheduled: {label: '待首次联系', badge: 'badge-gray'},
  scheduled: {label: '等待补件', badge: 'badge-blue'},
};
const channels = {phone: '电话', sms: '短信', email: '邮件'};
const triageDateTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short',
});

export async function renderDocumentTriage(user, navigate) {
  const {items, counts, business_date: today} = await get('/document-triage');
  const summary = Object.entries(queues).map(([id, {label, badge}]) => `
    <button class="triage-filter" type="button" data-bucket="${id}" aria-pressed="false">
      <span class="badge ${badge}">${label}</span><strong>${counts[id]}</strong>
    </button>`).join('');
  const content = `
    <header class="page-head">
      <div><h1 class="page-title">补件调度</h1>
      <p class="page-subtitle">先跟进已逾期和今日到期的案件。资料核齐后，案件离开队列并进入风险分级。</p></div>
      <div class="triage-date"><span>北京时间</span><b>${dateText(today)}</b></div>
    </header>
    ${Section('待补件案件', `
      <div class="triage-filters" role="group" aria-label="按补件进度筛选">${summary}</div>
      <div class="triage-list" id="triage-list">${items.map(renderQueueItem).join('')}</div>
      <p class="empty" id="triage-empty" ${items.length ? 'hidden' : ''}>当前没有符合条件的案件。受理缺少资料的新案件后会出现在这里。</p>`,
      `<button type="button" class="btn btn-line triage-filter-all" data-bucket="all" aria-pressed="true">显示全部 · ${items.length}</button>`)}
  `;
  setTimeout(() => bindQueue(items, today, navigate), 0);
  return Layout({user, active: 'documentTriage', onNavigate: navigate, content});
}

function renderQueueItem(item) {
  const {followup: record, bucket, claim_id: claimId} = item;
  const {label, badge} = queues[bucket];
  const previous = !record && item.latest_batch ? item.latest_batch : null;
  return `<article class="triage-row" data-queue="${bucket}">
    <div class="triage-identity">
      <button class="triage-claim" data-nav="detail" data-detail="${claimId}">${escapeHtml(item.claim_no)}</button>
      <strong>${escapeHtml(item.customer_name)}</strong>
      <span class="subline">${money(item.loss_amount)} · ${escapeHtml(item.claimant_phone)}</span>
    </div>
    <div class="triage-materials">
      <span class="subline">当前缺项</span>
      <ul>${item.missing_documents.map(doc => `<li>${escapeHtml(doc)}</li>`).join('')}</ul>
      ${renderReceiptLog(item)}
    </div>
    <div class="triage-deadline">
      <span class="badge ${badge}">${label}</span>
      ${record ? `<strong>补件期限 ${dateText(record.due_on)}</strong>
        <span class="subline">第 ${record.batch_no} 批催件 · ${dateText(record.contacted_on)} · ${channels[record.channel]} · ${escapeHtml(item.recorded_by_name)}</span>
        <p>${escapeHtml(record.contact_note)}</p>`
        : previous ? `<span class="subline">第 ${previous.batch_no} 批催件已作废</span>
        <p>误验撤销后需重新登记催件。</p>`
        : '<span class="subline">尚未约定补件日期</span>'}
    </div>
    <div class="triage-actions">
      <button type="button" class="btn btn-soft" data-contact="${claimId}">${record ? '登记再次联系' : previous ? '重新登记催件' : '登记首次联系'}</button>
      <button type="button" class="btn btn-line" data-receive="${claimId}">登记收件</button>
      <button type="button" class="btn btn-line" data-nav="detail" data-detail="${claimId}">进入资料核验</button>
    </div>
  </article>`;
}

function renderReceiptLog(item) {
  const receipts = item.receipts || [];
  if (!receipts.length) return '';
  const rows = receipts.map(receipt => {
    const batch = receipt.batch_no ? `第 ${receipt.batch_no} 批` : '未归属批次';
    const revoked = Boolean(receipt.revoked_at);
    return `<li class="${revoked ? 'revoked' : ''}">
      <span class="receipt-no">${escapeHtml(receipt.receipt_no)}</span>
      <span>${batch} · ${dateText(receipt.received_on)} · ${receipt.documents.map(escapeHtml).join('、')}</span>
      ${revoked
        ? `<span class="badge badge-gray">已撤销</span>`
        : `<button type="button" class="link-button" data-revoke="${item.claim_id}:${receipt.id}">撤销误验</button>`}
    </li>`;
  }).join('');
  return `<details class="triage-receipts">
    <summary>收件记录 · ${receipts.length} 次</summary>
    <ul class="receipt-list">${rows}</ul>
  </details>`;
}

function bindQueue(items, today, navigate) {
  const list = document.querySelector('#triage-list');
  if (!list) return;
  const filters = document.querySelectorAll('[data-bucket]');
  const rows = list.querySelectorAll('[data-queue]');
  for (const filter of filters) {
    filter.onclick = () => {
      const bucket = filter.dataset.bucket;
      filters.forEach(button => button.setAttribute('aria-pressed', button === filter));
      rows.forEach(row => {row.hidden = bucket !== 'all' && row.dataset.queue !== bucket;});
      document.querySelector('#triage-empty').hidden = Array.from(rows).some(row => !row.hidden);
    };
  }
  list.querySelectorAll('[data-contact]').forEach(button => {
    button.onclick = () => {
      const item = items.find(item => item.claim_id === Number(button.dataset.contact));
      openContactForm(item, today, navigate);
    };
  });
  list.querySelectorAll('[data-receive]').forEach(button => {
    button.onclick = () => {
      const item = items.find(item => item.claim_id === Number(button.dataset.receive));
      openReceiptForm(item, today, navigate);
    };
  });
  list.querySelectorAll('[data-revoke]').forEach(button => {
    button.onclick = () => {
      const [claimId, receiptId] = button.dataset.revoke.split(':').map(Number);
      const item = items.find(item => item.claim_id === claimId);
      openRevokeForm(item, receiptId, navigate);
    };
  });
}

function openReceiptForm(item, today, navigate) {
  const record = item.followup;
  const batchLine = record
    ? `本次收件默认归入第 ${record.batch_no} 批催件（联系日期 ${dateText(record.contacted_on)}）。早于该日期的晚到件请填写实际收件日期。
    `
    : '本案尚未登记催件联系，收件将只记录台账。<br>';
  const modal = openModal('登记收件', `
    <form class="form-grid triage-form">
      <p class="notice full">${batchLine}当前缺项：${item.missing_documents.map(escapeHtml).join('、')}</p>
      <div class="field"><label for="receipt-no">收件号</label>
        <input name="receipt_no" id="receipt-no" maxlength="64" placeholder="例如 RCV-2026-1108-01"></div>
      <div class="field"><label for="receipt-on">收件日期</label>
        <input type="date" name="received_on" id="receipt-on" value="${today}" max="${today}" required></div>
      <div class="field full"><label>收到的材料</label>
        <div class="checklist">${item.missing_documents.map(doc => `
          <label class="check-item"><input type="checkbox" name="docs" value="${escapeHtml(doc)}"> ${escapeHtml(doc)}</label>`).join('')}</div></div>
      <div class="field full"><label for="receipt-batch">归属催件批次</label>
        <select name="followup_id" id="receipt-batch">
          <option value="">按收件日期自动归属</option>
          ${(item.batches || []).map(batch => `<option value="${batch.id}">第 ${batch.batch_no} 批 · ${dateText(batch.contacted_on)}</option>`).join('')}
        </select></div>
      <p class="triage-error full" role="alert" hidden></p>
      <div class="full"><button type="submit" class="btn btn-primary">保存收件记录</button></div>
    </form>`);
  const form = modal.querySelector('form');
  form.onsubmit = async event => {
    event.preventDefault();
    const save = form.querySelector('[type=submit]');
    const error = form.querySelector('[role=alert]');
    const documents = [...form.querySelectorAll('input[name=docs]:checked')].map(item => item.value);
    if (!documents.length) {
      error.textContent = '请至少勾选一件收到的材料';
      error.hidden = false;
      return;
    }
    save.disabled = true;
    error.hidden = true;
    try {
      const data = Object.fromEntries(new FormData(form));
      delete data.docs;
      const result = await post(`/claims/${item.claim_id}/document-receipts`, {
        documents,
        received_on: data.received_on,
        receipt_no: data.receipt_no || null,
        followup_id: data.followup_id ? Number(data.followup_id) : null,
      });
      modal.remove();
      showToast(result.idempotent ? `收件号 ${result.receipt.receipt_no} 已登记过，未重复记收件` : `收件记录已保存 · ${result.receipt.receipt_no}`);
      navigate('documentTriage');
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      save.disabled = false;
    }
  };
}

function openRevokeForm(item, receiptId, navigate) {
  const receipt = (item.receipts || []).find(row => row.id === receiptId);
  if (!receipt) return;
  const modal = openModal('撤销误验', `
    <form class="form-grid triage-form">
      <p class="notice full">撤销收件记录 <b>${escapeHtml(receipt.receipt_no)}</b>（${receipt.documents.map(escapeHtml).join('、')}）。
      撤销后材料重新计入缺项，本批催件将作废，需要重新登记催件。</p>
      <div class="field full"><label for="revoke-note">撤销原因</label>
        <textarea name="note" id="revoke-note" minlength="6" maxlength="500" required placeholder="说明误验的材料与原因"></textarea></div>
      <p class="triage-error full" role="alert" hidden></p>
      <div class="full"><button type="submit" class="btn btn-danger">确认撤销并重新催件</button></div>
    </form>`);
  const form = modal.querySelector('form');
  form.onsubmit = async event => {
    event.preventDefault();
    const save = form.querySelector('[type=submit]');
    const error = form.querySelector('[role=alert]');
    save.disabled = true;
    error.hidden = true;
    try {
      await post(`/claims/${item.claim_id}/document-receipts/${receiptId}/revoke`, {
        note: form.elements.note.value,
      });
      modal.remove();
      showToast('误验已撤销，请重新登记催件');
      navigate('documentTriage');
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      save.disabled = false;
    }
  };
}

function openContactForm(item, today, navigate) {
  const record = item.followup;
  const modal = openModal(record ? '登记再次联系' : '登记首次联系', `
    <form class="form-grid triage-form">
      <p class="notice full"><b>${escapeHtml(item.claim_no)}</b><br>本次催收 ${item.missing_documents.map(escapeHtml).join('、')}</p>
      <div class="field full"><label for="triage-channel">联系渠道</label><select name="channel" id="triage-channel">
        ${Object.entries(channels).map(([key, label]) => `<option value="${key}">${label}</option>`).join('')}
      </select></div>
      <div class="field"><label for="triage-contacted">联系日期</label>
        <input type="date" name="contacted_on" id="triage-contacted" value="${today}" max="${today}" ${record ? `min="${dateText(record.contacted_on)}"` : ''} required></div>
      <div class="field"><label for="triage-due">补件期限</label>
        <input type="date" name="due_on" id="triage-due" min="${today}" required></div>
      <div class="field full"><label for="triage-note">联系结果</label>
        <textarea name="contact_note" id="triage-note" required minlength="6" maxlength="500" placeholder="记录沟通情况和约定的交付方式"></textarea></div>
      <p class="triage-error full" role="alert" hidden></p>
      <div class="full"><button type="submit" class="btn btn-primary">保存补件记录</button></div>
    </form>`);
  const form = modal.querySelector('form');
  form.elements.contacted_on.onchange = () => {form.elements.due_on.min = form.elements.contacted_on.value;};
  form.onsubmit = async event => {
    event.preventDefault();
    const save = form.querySelector('[type=submit]');
    const error = form.querySelector('[role=alert]');
    save.disabled = true;
    error.hidden = true;
    try {
      await post(`/claims/${item.claim_id}/document-followup`, {
        ...Object.fromEntries(new FormData(form)),
        followup_id: record?.id ?? null,
        expected_revision: record?.revision ?? 0,
        expected_missing_documents: item.missing_documents,
      });
      modal.remove();
      showToast('补件记录已保存');
      navigate('documentTriage');
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      save.disabled = false;
    }
  };
}

export function renderDocumentFollowup(user, claim, record, batches = [], receipts = []) {
  if (!canTriage(user.role)) return '';
  const canFollowUp = ['intake', 'initial_review'].includes(claim.status)
    && claim.required_documents.some(doc => !claim.received_documents.includes(doc));
  if (!canFollowUp && !record && !batches.length && !receipts.length) return '';
  const link = canFollowUp ? '<button class="btn btn-line" data-nav="documentTriage">打开补件调度</button>' : '';
  const complete = Boolean(record?.completed_at);
  const superseded = Boolean(record?.superseded_at);
  const statusLabel = superseded
    ? `第 ${record.batch_no} 批已作废`
    : complete ? (canFollowUp ? '上次已核齐' : '已核齐') : record ? `第 ${record.batch_no} 批催件中` : '待补件';
  const badgeTone = superseded ? 'badge-gray' : complete ? 'badge-mint' : 'badge-amber';
  const batchTrail = batches.length > 1
    ? `<div class="batch-trail"><span class="subline">催件批次</span>
        ${batches.map(batch => `<span class="badge ${batch.completed_at ? 'badge-mint' : batch.superseded_at ? 'badge-gray' : 'badge-amber'}">第 ${batch.batch_no} 批 · ${dateText(batch.contacted_on)}${batch.completed_at ? ' · 已核齐' : batch.superseded_at ? ' · 已作废' : ''}</span>`).join('')}
      </div>`
    : '';
  const receiptLog = receipts.length
    ? `<div class="receipt-history"><span class="subline">收件台账 · 每次收件归属批次</span>
        <ul class="receipt-list">${receipts.map(receipt => `<li class="${receipt.revoked_at ? 'revoked' : ''}">
          <span class="receipt-no">${escapeHtml(receipt.receipt_no)}</span>
          <span>${receipt.batch_no ? `第 ${receipt.batch_no} 批` : '未归属批次'} · 收件 ${dateText(receipt.received_on)} · ${receipt.documents.map(escapeHtml).join('、')}</span>
          <span class="subline">${escapeHtml(receipt.recorded_by_name)}</span>
          ${receipt.revoked_at ? '<span class="badge badge-gray">已撤销</span>' : ''}
        </li>`).join('')}</ul></div>`
    : '';
  const body = record ? `<div class="triage-receipt" aria-label="补件回执">
    <span class="badge ${badgeTone}">${statusLabel}</span>
    ${batchTrail}
    <dl class="review-fields">
      <div><dt>跟进记录</dt><dd>第 ${record.batch_no} 批 · 第 ${record.revision} 次 · ${channels[record.channel]} · ${escapeHtml(record.recorded_by_name)}</dd></div>
      <div><dt>约定期限</dt><dd>${dateText(record.due_on)}</dd></div>
      <div><dt>上次催收</dt><dd>${record.requested_documents.map(escapeHtml).join('、')}</dd></div>
      ${complete ? `<div><dt>核齐时间与经办人</dt><dd>${triageDateTime.format(new Date(record.completed_at))} · ${escapeHtml(record.completed_by_name)}</dd></div>` : ''}
    </dl>
    <p>${escapeHtml(record.contact_note)}</p>
    ${receiptLog}
    ${complete && canFollowUp ? '<div class="notice">资料清单已更正，当前缺项待联系。</div>' : ''}
    ${complete && claim.status === 'initial_review' && !canFollowUp ? '<div class="notice">所需资料已核齐，风险主管可以运行分级。</div>' : ''}
  </div>` : `${receiptLog || ''}<p class="subline">尚无联系记录。前往补件调度登记催收情况与期限。</p>`;
  return Section('补件跟进', body, link);
}
