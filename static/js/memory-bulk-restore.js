/* Multi-select archived memory restore. Preview only until explicit confirmation. */
let _bulkRestorePending = null;

function closeBulkRestoreModal() {
    document.getElementById('bulkRestoreModal').style.display = 'none';
    _bulkRestorePending = null;
}

async function openBulkRestorePreview() {
    const selected = [...document.querySelectorAll('.mem-check:checked')]
        .map(el => Number(el.value)).filter(Number.isSafeInteger);
    if (!selected.length) {
        showManageMsg('error', '先在「只看归档」里勾选要恢复的原始碎片');
        return;
    }
    if (selected.length > 500) {
        showManageMsg('error', '一次最多恢复500条；请分批操作');
        return;
    }
    const modal = document.getElementById('bulkRestoreModal');
    const overview = document.getElementById('bulkRestoreOverview');
    const items = document.getElementById('bulkRestoreItems');
    const submit = document.getElementById('bulkRestoreSubmit');
    modal.style.display = 'flex';
    submit.disabled = true;
    overview.textContent = '正在校验所选碎片，尚未修改记忆…';
    items.replaceChildren();
    _bulkRestorePending = null;
    try {
        const response = await fetch('/api/memories/batch-restore/preview', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ids:selected})
        });
        const data = await response.json();
        if (!response.ok || data.error) throw Error(data.detail || data.error || 'HTTP ' + response.status);
        const invalid = [...data.missing_ids, ...data.already_active_ids, ...data.other_layer_ids];
        overview.textContent =
            '选中 '+data.selected+'条；可恢复 '+data.restorable+
            '条；不存在 '+data.missing_ids.length+'条；已活跃 '+data.already_active_ids.length+
            '条；非原始碎片 '+data.other_layer_ids.length+'条。'+
            '关联事件 '+data.associated_events.length+'条碎片（事件本身不会删除）。';
        const linked = new Map(data.associated_events.map(item => [item.memory_id, item.event_ids]));
        for (const entry of data.preview_memories) {
            const item = document.createElement('div');
            item.style.cssText = 'border-bottom:1px solid var(--border,#ddd);padding:9px 4px;';
            const related = linked.get(entry.id);
            item.textContent = '#'+entry.id+' · '+entry.created_at+
                (related ? ' · 已并入事件 #'+related.join(', #') : '')+
                '\n'+entry.content;
            items.appendChild(item);
        }
        if (invalid.length) {
            const warning = document.createElement('p');
            warning.textContent = '部分ID不可恢复：'+invalid.join('、')+'。为防止部分成功，本次提交被禁用；请重新筛选。';
            items.prepend(warning);
        }
        if (!invalid.length && data.restorable === selected.length) {
            _bulkRestorePending = {ids:selected, state_token:data.state_token};
            submit.disabled = false;
        }
    } catch(e) {
        overview.textContent = '预览失败，未修改任何记忆：'+e.message;
    }
}

async function confirmBulkRestore() {
    if (!_bulkRestorePending) return;
    const pending = _bulkRestorePending;
    if (!window.confirm('确定恢复 '+pending.ids.length+
        ' 条归档碎片为AI可检索状态？合并事件会保留，可能增加重复检索。')) return;
    const button = document.getElementById('bulkRestoreSubmit');
    button.disabled = true;
    try {
        const response = await fetch('/api/memories/batch-restore/confirm', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({...pending, confirmation:'确认批量恢复'})
        });
        const data = await response.json();
        if (!response.ok || data.error) throw Error(data.detail || data.error || 'HTTP ' + response.status);
        closeBulkRestoreModal();
        clearSelection();
        await loadMemories();
        showManageMsg('success', '已恢复 '+data.restored+' 条原始碎片，合并事件保留');
    } catch(e) {
        const overview = document.getElementById('bulkRestoreOverview');
        overview.textContent = '恢复未完成：'+e.message+'。请关闭并重新预览确认。';
        _bulkRestorePending = null;
    } finally {
        button.disabled = false;
    }
}
