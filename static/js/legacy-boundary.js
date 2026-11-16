/* Legacy summary cutoff: preview-only until the user explicitly confirms. */
(() => {
    let activeSession = '';
    let stateToken = '';
    let nextBeforeId = null;
    let chosenId = null;

    function setNote(value) {
        const el = document.getElementById('legacy-boundary-note');
        if (el) el.textContent = value;
    }

    async function requestJson(url, options) {
        const response = await fetch(url, options);
        const data = await response.json();
        if (!response.ok || data.error) {
            throw new Error(data.detail || data.error || 'HTTP_' + response.status);
        }
        return data;
    }

    async function load(beforeId = null) {
        const status = await requestJson('/api/partition/status');
        activeSession = status.active_session_id || '';
        if (!activeSession) {
            setNote('当前没有活跃对话线。');
            return;
        }
        const params = new URLSearchParams({session_id: activeSession, limit: '60'});
        if (beforeId !== null) params.set('before_id', String(beforeId));
        setNote('正在读取当前对话线的候选消息（只读）…');
        const result = await requestJson('/api/partition/boundary/candidates?' + params);
        const area = document.getElementById('legacy-boundary-candidates');
        const confirmButton = document.getElementById('legacy-boundary-confirm');
        if (!area) return;
        area.replaceChildren();
        stateToken = result.state_token;
        chosenId = null;
        nextBeforeId = result.next_before_id;
        confirmButton.hidden = !result.requires_alignment;
        if (!result.requires_alignment) {
            setNote('这条对话线已有消息ID检查点，不需要再次校准。');
            return;
        }
        setNote('请找出「现有摘要结束后，第一条尚未被摘要的用户消息」。选择前请核对内容与时间。操作不会重新生成旧摘要。');
        for (const candidate of result.candidates) {
            const label = document.createElement('label');
            label.style.cssText = 'display:flex;gap:9px;align-items:flex-start;padding:9px 4px;border-bottom:1px solid var(--border-color,#ddd);cursor:pointer;';
            const radio = document.createElement('input');
            radio.type = 'radio';
            radio.name = 'legacyBoundaryCandidate';
            radio.value = String(candidate.id);
            radio.addEventListener('change', () => { chosenId = candidate.id; });
            const description = document.createElement('span');
            description.textContent = '#' + candidate.id + ' · ' + candidate.created_at + ' · ' + (candidate.preview || '[纯图片或空文字]');
            label.append(radio, description);
            area.appendChild(label);
        }
        if (nextBeforeId !== null) {
            const older = document.createElement('button');
            older.type = 'button';
            older.className = 'btn btn-sm';
            older.textContent = '更早的消息';
            older.addEventListener('click', () => load(nextBeforeId).catch(e => setNote('读取失败：' + e.message)));
            area.appendChild(older);
        }
        if (!result.candidates.length) setNote('没有找到可用的用户消息；保持现有摘要不变。');
    }

    async function confirm() {
        if (!chosenId) {
            setNote('请先选择第一条尚未摘要的用户消息。');
            return;
        }
        if (!window.confirm('确定第 #' + chosenId + ' 条是「第一条尚未摘要的用户消息」？将先保存旧摘要快照，只更新检查点。选错会导致摘要重复或跳过。')) return;
        const button = document.getElementById('legacy-boundary-confirm');
        button.disabled = true;
        try {
            const result = await requestJson('/api/partition/boundary/confirm', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    session_id: activeSession,
                    first_unsummarized_user_id: chosenId,
                    state_token: stateToken,
                    confirmation: '确认摘要截止位置'
                })
            });
            setNote('已校准。原有摘要未改动；回滚快照 #' + result.snapshot_id + '。后续继续按每8轮的分区规则处理。');
            const area = document.getElementById('legacy-boundary-candidates');
            if (area) area.replaceChildren();
            button.hidden = true;
            if (typeof loadThreads === 'function') await loadThreads();
        } catch (error) {
            setNote('校准失败，未更改摘要：' + error.message + '。请重新读取候选消息。');
        } finally {
            button.disabled = false;
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        const readButton = document.getElementById('legacy-boundary-read');
        const confirmButton = document.getElementById('legacy-boundary-confirm');
        if (readButton) readButton.addEventListener('click', () => load().catch(e => setNote('读取失败：' + e.message)));
        if (confirmButton) confirmButton.addEventListener('click', confirm);
    });
})();
