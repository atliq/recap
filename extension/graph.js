document.addEventListener('DOMContentLoaded', () => {
    function esc(str) {
        return String(str ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // Only allow http(s) navigation targets - blocks javascript:/data: URLs.
    function safeUrl(u) {
        return /^https?:\/\//i.test(String(u)) ? String(u) : '#';
    }

    const loading = document.getElementById('loading');
    const panel = document.getElementById('info-panel');
    const pTitle = document.getElementById('panel-title');
    const pType = document.getElementById('panel-type');
    const pConn = document.getElementById('panel-connections');

    // Group colours: 1=person/org entity, 2=other entity, 3=domain hub, 4=page
    const GROUP_COLORS = {
        1: '#7c5cff',  // purple - person/org (deepened for light bg)
        2: '#0c8f7f',  // teal   - other entity (theme accent)
        3: '#b45309',  // amber  - domain (theme amber; visible on light)
        4: '#2563eb',  // blue   - page (deepened for light bg)
    };

    const GROUP_LABELS = {
        1: 'Person / Org',
        2: 'Entity',
        3: 'Domain',
        4: 'Page',
    };

    chrome.storage.sync.get(['apiUrl'], async (result) => {
        const apiBase = result.apiUrl || 'http://localhost:8000';
        try {
            const res = await fetch(`${apiBase}/graph`);
            if (!res.ok) throw new Error("Failed to fetch graph");
            const data = await res.json();

            loading.style.display = 'none';

            if (!data.nodes || data.nodes.length === 0) {
                loading.style.display = 'block';
                loading.textContent = 'No data yet - browse some pages first.';
                return;
            }

            const Graph = ForceGraph3D()
                (document.getElementById('3d-graph'))
                .backgroundColor('#e8ebf0')
                .graphData(data)
                .nodeLabel(n => esc(n.name))
                .nodeColor(node => GROUP_COLORS[node.group] || '#8b8b9e')
                .nodeVal(node => {
                    // Domain hubs are bigger, pages medium, entities by frequency
                    if (node.group === 3) return 8;
                    if (node.group === 4) return Math.sqrt(node.val || 1) * 1.5 + 2;
                    return Math.sqrt(node.val || 1) * 2;
                })
                .linkWidth(link => Math.sqrt(link.value || 1) * 0.5)
                .linkColor(link => {
                    // dark links on light paper; page→domain subtle, entity links stronger
                    const src = typeof link.source === 'object' ? link.source : {};
                    if (src.group === 4 || src.group === 3) return 'rgba(22,32,43,0.12)';
                    return 'rgba(22,32,43,0.28)';
                })
                .onNodeClick(node => {
                    // If it's a page node, open the URL on double-click
                    if (node.group === 4 && node.url) {
                        window.open(safeUrl(node.url), '_blank');
                        return;
                    }

                    // Pause auto-rotation so it doesn't fight the click-to-focus move
                    stopRotation();

                    // Focus camera on node (guard against undefined coords pre-layout → NaN)
                    const x = node.x || 0, y = node.y || 0, z = node.z || 0;
                    const distance = 80;
                    const distRatio = 1 + distance / Math.hypot(x || 1, y || 1, z || 1);
                    Graph.cameraPosition(
                        { x: x * distRatio, y: y * distRatio, z: z * distRatio },
                        node,
                        3000
                    );

                    // Update side panel
                    pTitle.textContent = node.name;
                    pType.textContent = GROUP_LABELS[node.group] || 'Node';

                    // Find direct connections
                    const connections = data.links
                        .filter(l => {
                            const sid = typeof l.source === 'object' ? l.source.id : l.source;
                            const tid = typeof l.target === 'object' ? l.target.id : l.target;
                            return sid === node.id || tid === node.id;
                        })
                        .map(l => {
                            const sid = typeof l.source === 'object' ? l.source.id : l.source;
                            return sid === node.id
                                ? (typeof l.target === 'object' ? l.target : { id: l.target, name: l.target })
                                : (typeof l.source === 'object' ? l.source : { id: l.source, name: l.source });
                        })
                        .slice(0, 6);

                    if (connections.length > 0) {
                        pConn.innerHTML = 'Connected to:<br>' + connections.map(c =>
                            `<div class="node-connection-item">${esc(c.name || c.id)}</div>`
                        ).join('');
                    } else {
                        pConn.innerHTML = 'No direct links.';
                    }

                    panel.classList.add('visible');
                })
                .onBackgroundClick(() => {
                    panel.classList.remove('visible');
                    startRotation();
                });

            // Subtle slow rotation - stored so it can be paused on node focus
            let angle = 0;
            let rotationTimer = null;

            function startRotation() {
                if (rotationTimer) return;
                rotationTimer = setInterval(() => {
                    Graph.cameraPosition({
                        x: 200 * Math.sin(angle),
                        z: 200 * Math.cos(angle)
                    });
                    angle += Math.PI / 1200;
                }, 30);
            }

            function stopRotation() {
                if (rotationTimer) {
                    clearInterval(rotationTimer);
                    rotationTimer = null;
                }
            }

            startRotation();

        } catch (e) {
            loading.textContent = 'Could not load graph. Is the backend running?';
        }
    });
});