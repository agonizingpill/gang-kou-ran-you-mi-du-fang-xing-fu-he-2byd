import { heading } from '../components/logbook.js';
import { esc, formData, localTime } from '../utils/format.js';
import { api } from '../utils/api.js';

const states = { training: '训练中', ready: '双等级已就绪', failed: '训练失败' };
const events = {
  created: '建立候选方案',
  trained: '双等级训练完成',
  train_failed: '训练失败',
  retry: '重试训练',
  publish: '主管发布',
  rollback: '主管回滚',
  bootstrap: '首次部署启用基线',
};

function tags(x) {
  return [
    `<span class="tag ${x.train_state === 'ready' ? 'qualified' : x.train_state === 'failed' ? 'rejected' : 'training'}">${states[x.train_state] || esc(x.train_state)}</span>`,
    x.is_active ? '<span class="tag active">在用版本</span>' : '',
    x.published_at ? `<span class="tag">${esc(x.published_by || '')} 已发布</span>` : '',
  ].join(' ');
}

function modelRows(x) {
  return (x.models || [])
    .map(m => `<tr><td>${m.grade}</td><td>${m.reference_count}</td><td>${m.threshold == null ? '—' : Number(m.threshold).toFixed(3)}</td><td>${m.ready && m.artifact_ok ? '产物已校验' : esc(m.error || '未就绪')}</td></tr>`)
    .join('');
}

function referencePicker(s, grade, selected) {
  const rows = (s.referenceSamples || []).filter(x => x.grade === grade);
  const chosen = new Set(selected || []);
  return `<label>${grade} 参考样（${rows.length} 条可挑选，至少 20 条）<button type="button" class="ghost tiny-pick" data-pick="${grade}">全选本等级</button></label><select name="${grade}_refs" multiple size="8">${rows
    .map(x => `<option value="${x.id}" ${chosen.has(x.id) ? 'selected' : ''}>${esc(x.code)} · ${x.density_15}</option>`)
    .join('')}</select>`;
}

function diffPanel(d) {
  if (!d) return '<div class="empty">选择一个有新训练产物的候选方案，查看与在用方案的参考集和判定差异。</div>';
  const grades = ['VLSFO', 'MGO']
    .filter(g => d.grades[g])
    .map(g => {
      const x = d.grades[g];
      if (!x.ready) return `<div class="sheet"><h3>${g}</h3><p class="tiny">新方案该等级尚未训练完成，无法比较。</p></div>`;
      const changes = [...x.added.map(r => `<li>新增 ${esc(r.code)} · ${r.density_15}</li>`), ...x.removed.map(r => `<li>移出 ${esc(r.code)} · ${r.density_15}</li>`), ...x.changed.map(r => `<li>数值变动 ${esc(r.code)} · ${r.old_density_15} → ${r.new_density_15}</li>`)];
      return `<div class="sheet"><h3>${g} 参考集与阈值</h3><div class="readings"><div class="reading"><small>参考数 旧 → 新</small><b>${x.old_count ?? '—'} → ${x.new_count}</b></div><div class="reading"><small>5% 阈值 旧 → 新</small><b>${x.threshold_old == null ? '—' : Number(x.threshold_old).toFixed(3)} → ${Number(x.threshold_new).toFixed(3)}</b></div><div class="reading"><small>参照油样判定变化</small><b>${d.decisions.items.filter(i => i.grade === g && i.changed).length} / ${d.decisions.items.filter(i => i.grade === g).length}</b></div></div><ul class="tiny">${changes.join('') || '<li>参考集未增删，数值未变</li>'}</ul></div>`;
    })
    .join('');
  const rows = d.decisions.items
    .map(i => `<tr><td><b>${esc(i.sample_code)}</b></td><td>${i.grade}</td><td>${i.density_15}</td><td>${i.old_score == null ? '—' : i.old_score.toFixed(3)}</td><td>${i.new_score.toFixed(3)}</td><td>${i.old_passed == null ? '无旧方案' : i.old_passed ? '合格' : '异常'}</td><td>${i.new_passed ? '合格' : '异常'}</td><td>${i.changed == null ? '—' : i.changed ? '<b style="color:var(--red)">判定变化</b>' : '不变'}</td></tr>`)
    .join('');
  return `<div class="sheet"><div class="transfer-head"><h2>${esc(d.plan_code)} 对照 ${esc(d.against_plan_code || '无基线')}</h2><span class="tag">判定变化 ${d.decisions.changed} / ${d.decisions.total}</span></div>${grades}<p class="note">${esc(d.note)}</p><table><thead><tr><th>油样</th><th>等级</th><th>密度</th><th>旧分数</th><th>新分数</th><th>旧判定</th><th>新判定</th><th>差异</th></tr></thead><tbody>${rows || '<tr><td colspan="8">暂无业务油样可重算。</td></tr>'}</tbody></table></div>`;
}

export function render(s, u) {
  const p = s.plans || { plans: [], revision: 0 };
  const list = p.plans || [];
  const chosen = (s.plansSelected && list.find(a => a.id === s.plansSelected))
    || (p.active_plan && list.find(a => a.id === p.active_plan.id))
    || list[0];
  const baseline = chosen && chosen.baseline_plan_id ? p.plans.find(a => a.id === chosen.baseline_plan_id) : p.active_plan;
  const refSel = chosen?.reference_samples || {};
  return (
    heading('10', '参考库换版，判定可复现。', '候选方案固定所选参考样，双等级全部训练成功才可发布；发布与回滚以在用版本号校验，过期审批会被拒绝。')
    + `<div class="supply-layout"><aside><h2>建立训练候选方案</h2><p class="tiny">当前在用：${p.active_plan ? esc(p.active_plan.code) + ' · rev ' + p.revision : '无（比对将暂停受理）'}</p><form id="plan-create"><label>候选方案编号<input name="code" placeholder="DP-20261117-01" required></label><label>方案说明<input name="note" placeholder="例如：加入11月新参考样" maxlength="600"></label><label>对照基线方案<select name="baseline_plan_id"><option value="">当前在用版本</option>${(p.plans || []).map(a => `<option value="${a.id}">${esc(a.code)} · ${states[a.train_state]}</option>`).join('')}</select></label>${referencePicker(s, 'VLSFO', refSel.VLSFO?.map(r => r.sample_id))}${referencePicker(s, 'MGO', refSel.MGO?.map(r => r.sample_id))}<div class="actions"><button ${u.role !== 'admin' ? 'disabled' : ''}>快照参考样并训练</button></div></form><p class="note">参考样只能取自现有 reference 样，一个参考样不能跨等级重复。任一等级不足 20 条或训练失败，整份方案不可发布，在用版本保持不变。</p><div class="receipt-index">${(p.plans || []).map(a => `<button class="receipt-pick" data-plan="${a.id}"><b>${esc(a.code)}</b><small>${states[a.train_state]} / rev ${a.train_epoch}${a.is_active ? ' / 在用' : ''}</small></button>`).join('')}</div></aside><section>${chosen ? `<div class="sheet"><div class="transfer-head"><h2>${esc(chosen.code)}</h2><div>${tags(chosen)}</div></div><p class="tiny">${esc(chosen.note)} · 建立人 ${esc(chosen.created_by)} · 基线 ${esc(baseline?.code || '无')}${chosen.error ? ' · ' + esc(chosen.error) : ''}</p><table><thead><tr><th>等级</th><th>参考数</th><th>5% 阈值</th><th>训练产物</th></tr></thead><tbody>${modelRows(chosen)}</tbody></table><div class="actions"><button type="button" id="plan-diff" data-id="${chosen.id}">查看版本差异</button><button type="button" id="plan-retrain" data-id="${chosen.id}" ${u.role !== 'admin' || !chosen.can_retrain ? 'disabled' : ''}>重试训练</button><button type="button" class="copper" id="plan-publish" data-id="${chosen.id}" data-revision="${p.revision}" ${u.role !== 'admin' || !chosen.can_publish ? 'disabled' : ''}>主管发布（基于 rev ${p.revision}）</button><button type="button" id="plan-rollback" data-id="${chosen.id}" data-revision="${p.revision}" ${u.role !== 'admin' || !chosen.can_rollback ? 'disabled' : ''}>主管回滚到此版本</button></div><p class="note">发布与回滚提交时携带页面所见的在用版本号；期间若他人已换版，操作会被拒绝，须刷新后重新审批。已发布方案作为历史版本冻结，修改参考库须另建候选。</p><h3>版本事件</h3><div class="journal">${(chosen.events || []).map(e => `<p><b>${events[e.action] || esc(e.action)}</b><small>${localTime(e.at)} · ${esc(e.actor)} · rev ${e.revision}${e.detail?.error ? ' · ' + esc(e.detail.error) : ''}</small></p>`).join('') || '<p class="tiny">暂无事件</p>'}</div></div>${diffPanel(s.planDiff)}` : '<div class="empty">先建立候选方案，训练完成后可比较新旧判定差异。</div>'}</section></div>`
  );
}

function picked(form, grade) {
  return Array.from(form.querySelectorAll(`select[name="${grade}_refs"] option:checked`)).map(o => Number(o.value));
}

export function bind(refresh, render, s) {
  document.querySelectorAll('[data-plan]').forEach(b => b.onclick = () => { s.plansSelected = +b.dataset.plan; s.planDiff = null; render(); });
  document.querySelectorAll('[data-pick]').forEach(b => b.onclick = () => {
    const sel = document.querySelector(`select[name="${b.dataset.pick}_refs"]`);
    Array.from(sel.options).forEach(o => { o.selected = true; });
  });
  const c = document.querySelector('#plan-create');
  if (c) c.onsubmit = e => {
    e.preventDefault();
    const d = formData(c);
    refresh(async () => {
      const x = await api('/density-plans', 'POST', {
        code: d.code,
        note: d.note || '',
        baseline_plan_id: d.baseline_plan_id ? Number(d.baseline_plan_id) : null,
        references: { VLSFO: picked(c, 'VLSFO'), MGO: picked(c, 'MGO') },
      });
      s.plansSelected = x.id;
      s.planDiff = null;
    }, '候选方案已快照参考样，等待双等级训练');
  };
  const diff = document.querySelector('#plan-diff');
  if (diff) diff.onclick = () => refresh(async () => { s.planDiff = await api('/density-plans/' + diff.dataset.id + '/diff'); }, '版本差异已按当前业务油样重算');
  const r = document.querySelector('#plan-retrain');
  if (r) r.onclick = () => refresh(() => api('/density-plans/' + r.dataset.id + '/retrain', 'POST'), '已重新排入训练，将从断点补齐缺失产物');
  const p = document.querySelector('#plan-publish');
  if (p) p.onclick = () => refresh(async () => { await api('/density-plans/' + p.dataset.id + '/publish', 'POST', { expected_revision: Number(p.dataset.revision), note: '主管页面发布' }); s.planDiff = null; }, '双等级模型已整份发布，在用版本已切换');
  const b = document.querySelector('#plan-rollback');
  if (b) b.onclick = () => refresh(async () => { await api('/density-plans/' + b.dataset.id + '/rollback', 'POST', { expected_revision: Number(b.dataset.revision), note: '主管页面回滚' }); s.planDiff = null; }, '已回滚到选定历史版本');
}
