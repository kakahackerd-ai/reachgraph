(function () {
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // ---- status badges ----
  fetch('/api/status').then(function (r) { return r.json(); }).then(function (s) {
    if (s.guacEnabled) $('guac-badge').classList.add('on');
    if (s.githubTokenPresent) $('gh-badge').classList.add('on');
  }).catch(function () {});

  // ---- nav ----
  var views = { overview: $('view-overview'), repos: $('view-repos'), result: $('view-result') };
  function showView(name) {
    Object.keys(views).forEach(function (k) { views[k].hidden = true; });
    views[name].hidden = false;
    $('nav-overview').classList.toggle('active', name === 'overview');
    $('nav-repos').classList.toggle('active', name === 'repos');
    $('crumb').textContent = name === 'overview' ? 'Overview' : name === 'repos' ? 'Repositories' : $('crumb').textContent;
    $('content').scrollTop = 0;
  }
  $('nav-overview').addEventListener('click', function () { showView('overview'); });
  $('nav-repos').addEventListener('click', function () { showView('repos'); loadRepos('2'); });
  $('back-to-overview').addEventListener('click', function () { showView('overview'); });

  // ---- tabs ----
  $('tab-package').addEventListener('click', function () { setTab('package'); });
  $('tab-repo').addEventListener('click', function () { setTab('repo'); });
  function setTab(which) {
    $('tab-package').classList.toggle('active', which === 'package');
    $('tab-repo').classList.toggle('active', which === 'repo');
    $('form-package').style.display = which === 'package' ? 'flex' : 'none';
    $('form-repo').style.display = which === 'repo' ? 'flex' : 'none';
    $('scan-hint').textContent = which === 'package'
      ? 'Resolves the latest published version unless you type one, e.g. express@4.17.0.'
      : 'Reads package.json (and package-lock.json, if present) straight from the repository, e.g. lodash/lodash.';
  }

  // ---- toast ----
  var toastTimer;
  function showToast(text) {
    $('toast-text').textContent = text;
    $('toast').classList.add('open');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { $('toast').classList.remove('open'); }, 3200);
  }

  // ---- repositories ----
  function loadRepos(suffix) {
    var gridId = suffix === '2' ? 'repo-grid-2' : 'repo-grid';
    var emptyId = suffix === '2' ? 'repo-empty-2' : 'repo-empty';
    fetch('/api/repos').then(function (r) { return r.json(); }).then(function (d) {
      var grid = $(gridId), empty = $(emptyId), label = suffix === '1' ? $('tracked-label') : null;
      grid.innerHTML = '';
      if (!d.repos || d.repos.length === 0) {
        empty.style.display = 'block';
        empty.textContent = d.guacEnabled
          ? 'No repositories tracked yet — scan one above.'
          : 'GUAC_GRAPHQL_URL is not configured, so nothing persists between restarts. Scan a repository below to see it here for this session only.';
        if (label) label.style.display = 'none';
        return;
      }
      empty.style.display = 'none';
      if (label) label.style.display = 'block';
      d.repos.forEach(function (name) {
        var card = document.createElement('button');
        card.className = 'repo-card';
        card.innerHTML = '<div class="name">' + esc(name) + '</div><div class="re-scan">Click to re-scan</div>';
        card.addEventListener('click', function () {
          var parts = name.split('/');
          if (parts.length === 2) runRepoScan(parts[0], parts[1]);
        });
        grid.appendChild(card);
      });
    }).catch(function () {});
  }
  loadRepos('1');

  // ---- scan forms ----
  $('form-package').addEventListener('submit', function (e) {
    e.preventDefault();
    var raw = $('input-package').value.trim();
    if (!raw) return;
    var pkg = raw, version = '';
    var at = raw.lastIndexOf('@');
    if (at > 0) { pkg = raw.slice(0, at); version = raw.slice(at + 1); }
    runPackageScan(pkg, version);
  });
  $('form-repo').addEventListener('submit', function (e) {
    e.preventDefault();
    var raw = $('input-repo').value.trim();
    var parts = raw.split('/');
    if (parts.length !== 2 || !parts[0] || !parts[1]) { showStatusError('Enter a repository as owner/repo.'); return; }
    runRepoScan(parts[0], parts[1]);
  });

  function setLoading(on, text) {
    $('status-row').classList.toggle('visible', on);
    if (text) $('status-text').textContent = text;
    if (on) $('error-box').classList.remove('visible');
  }
  function showStatusError(msg) {
    $('error-box').textContent = msg;
    $('error-box').classList.add('visible');
    $('status-row').classList.remove('visible');
  }

  function runPackageScan(pkg, version) {
    setLoading(true, 'Resolving ' + pkg + ' from deps.dev…');
    fetch('/api/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ecosystem: 'npm', package: pkg, version: version }) })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) { setLoading(false); if (!res.ok) return showStatusError(res.d.error || 'scan failed'); renderResult(res.d); })
      .catch(function (err) { setLoading(false); showStatusError(String(err)); });
  }
  function runRepoScan(owner, repo) {
    setLoading(true, 'Reading ' + owner + '/' + repo + ' from GitHub…');
    fetch('/api/scan-repo', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ owner: owner, repo: repo }) })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) { setLoading(false); if (!res.ok) return showStatusError(res.d.error || 'scan failed'); renderResult(res.d); })
      .catch(function (err) { setLoading(false); showStatusError(String(err)); });
  }

  // ---- result rendering ----
  var cy = null;
  var currentData = null;
  var graphState = { rootId: null };

  var SEV_VAR = { Critical: '--crit', High: '--high', Medium: '--med', Low: '--good' };
  var SEV_HEX = { Critical: '#FF6259', High: '#FFA24D', Medium: '#F0CB4E', Low: '#4FCE84' };
  function severityClass(label) { return { Critical: 'sev-critical', High: 'sev-high', Medium: 'sev-medium', Low: 'sev-low' }[label] || 'sev-low'; }
  function severityVar(label) { return SEV_VAR[label] || '--good'; }

  function renderResult(data) {
    currentData = data;
    showView('result');
    $('crumb').textContent = data.subject.name;

    var versionSuffix = data.subject.version ? '@' + data.subject.version : '';
    $('result-title').textContent = data.subject.name + versionSuffix;

    var stats = [];
    stats.push('<span><b>' + data.totalPackages + '</b> packages</span>');
    stats.push('<span><b>' + data.directPackages + '</b> direct</span>');
    stats.push('<span><b>' + data.flaggedCount + '</b> flagged</span>');
    stats.push('<span><b>' + data.durationMs + 'ms</b> scan time</span>');
    if (data.source && data.source.dependencies) stats.push('<span>' + esc(data.source.dependencies) + '</span>');
    $('result-stats').innerHTML = stats.join('');

    if (data.paths && data.paths.length) {
      var top = data.paths[0];
      $('result-score').style.display = 'block';
      $('result-score-num').textContent = top.score.value;
      $('result-score-num').style.color = 'var(' + severityVar(top.score.label) + ')';
    } else {
      $('result-score').style.display = 'none';
    }

    renderGraph(data);
    renderPathList(data.paths || []);
    renderDependabot(data.dependabot);
    loadRepos('1');
  }

  function renderPathList(paths) {
    var list = $('path-list');
    list.innerHTML = '';
    if (!paths.length) {
      list.innerHTML = '<div class="dep-empty">No known vulnerabilities or malicious-package reports found.</div>';
      return;
    }
    paths.forEach(function (p, i) {
      var row = document.createElement('button');
      row.className = 'path-row';
      row.innerHTML = '<span class="sev ' + severityClass(p.score.label) + '">' + p.score.label + '</span>' +
        '<span class="title">' + esc(p.target.name) + '@' + esc(p.target.version) + '</span>' +
        '<span class="sc">' + p.score.value + '</span>';
      row.addEventListener('click', function () { selectPath(p, row, true); });
      list.appendChild(row);
      if (i === 0) selectPath(p, row, false);
    });
  }

  function selectPath(p, row, openPanel) {
    var rows = document.querySelectorAll('.path-row');
    for (var i = 0; i < rows.length; i++) rows[i].classList.remove('active');
    row.classList.add('active');
    focusPathInGraph(p);
    if (openPanel) openInspectorForTarget(p);
  }

  function renderDependabot(dep) {
    var body = $('dependabot-body');
    if (!dep) { body.innerHTML = '<div class="dep-empty">Package scans have no repository to check. Scan a repository to see this.</div>'; return; }
    if (dep.status === 'not_configured') { body.innerHTML = '<div class="dep-empty">' + esc(dep.detail) + '</div>'; return; }
    if (dep.status === 'error') { body.innerHTML = '<div class="dep-empty" style="color:var(--crit)">' + esc(dep.detail) + '</div>'; return; }
    if (!dep.alerts || !dep.alerts.length) { body.innerHTML = '<div class="dep-empty">GitHub reports no open Dependabot alerts for this repository.</div>'; return; }
    body.innerHTML = dep.alerts.map(function (a) {
      return '<div class="dep-item"><span class="pkg">' + esc(a.package) + '</span>' +
        (a.severity ? (' &middot; ' + esc(a.severity)) : '') +
        (a.url ? (' &middot; <a href="' + a.url + '" target="_blank" rel="noopener" style="color:var(--accent)">' + esc(a.ghsaId || 'view') + '</a>') : '') +
        (a.summary ? ('<p>' + esc(a.summary) + '</p>') : '') + '</div>';
    }).join('');
  }

  // ---- graph ----
  function nodeId(name, version) { return version ? (name + '@' + version) : name; }

  function buildElements(subject, paths) {
    var nodes = new Map();
    var edgeIds = new Set();
    var rootId = nodeId(subject.name, subject.kind === 'repository' ? '' : subject.version);
    nodes.set(rootId, { id: rootId, name: subject.name, version: subject.kind === 'repository' ? '' : (subject.version || ''), root: true });

    (paths || []).forEach(function (p) {
      for (var i = 0; i < p.hops.length; i++) {
        var h = p.hops[i];
        var id = nodeId(h.name, h.version);
        if (!nodes.has(id)) nodes.set(id, { id: id, name: h.name, version: h.version || '', root: id === rootId });
        if (i > 0) {
          var prev = p.hops[i - 1];
          var prevId = nodeId(prev.name, prev.version);
          edgeIds.add(prevId + '→' + id);
        }
      }
      var targetId = nodeId(p.target.name, p.target.version);
      var existing = nodes.get(targetId) || { id: targetId, name: p.target.name, version: p.target.version, root: false };
      existing.severity = p.score.label;
      nodes.set(targetId, existing);
    });

    var elements = [];
    nodes.forEach(function (n) {
      elements.push({ data: { id: n.id, name: n.name, version: n.version, root: n.root, flagged: !!n.severity, severity: n.severity || '' } });
    });
    edgeIds.forEach(function (e) {
      var parts = e.split('→');
      elements.push({ data: { id: 'e:' + parts[0] + '>' + parts[1], source: parts[0], target: parts[1] } });
    });
    return { elements: elements, rootId: rootId };
  }

  function graphLayout() {
    return { name: 'breadthfirst', roots: '[?root]', directed: true, animate: true, animationDuration: 500, spacingFactor: 1.25, padding: 30 };
  }

  function renderGraph(data) {
    var built = buildElements(data.subject, data.paths);
    graphState.rootId = built.rootId;

    if (cy) { cy.destroy(); cy = null; }
    cy = cytoscape({
      container: $('graph-canvas'),
      elements: built.elements,
      style: [
        { selector: 'node', style: {
          'background-color': function (ele) { return nodeColor(ele); },
          'border-width': 2,
          'border-color': function (ele) { return nodeColor(ele); },
          'label': 'data(name)',
          'color': '#E7ECEE',
          'font-size': 10,
          'font-family': 'SFMono-Regular, Menlo, monospace',
          'text-valign': 'bottom',
          'text-margin-y': 6,
          'width': function (ele) { return ele.data('root') ? 26 : 15; },
          'height': function (ele) { return ele.data('root') ? 26 : 15; },
          'shape': function (ele) { return ele.data('root') ? 'diamond' : 'ellipse'; },
        } },
        { selector: 'edge', style: {
          'width': 1.4,
          'line-color': '#2C363E',
          'target-arrow-color': '#2C363E',
          'target-arrow-shape': 'triangle',
          'arrow-scale': 0.8,
          'curve-style': 'bezier',
        } },
        { selector: '.highlighted', style: { 'line-color': '#35C6E8', 'target-arrow-color': '#35C6E8', 'width': 2.4, 'z-index': 10 } },
        { selector: 'node:selected', style: { 'border-width': 3 } },
      ],
      layout: graphLayout(),
      wheelSensitivity: 0.25,
      minZoom: 0.3,
      maxZoom: 3,
    });
    cy.on('tap', 'node', function (evt) { onNodeClick(evt.target); });
    $('graph-refit').onclick = function () { cy.animate({ fit: { eles: cy.elements(), padding: 30 }, duration: 300 }); };
  }

  function nodeColor(ele) {
    if (ele.data('root')) return '#35C6E8';
    var sev = ele.data('severity');
    return SEV_HEX[sev] || '#3A444C';
  }

  function focusPathInGraph(p) {
    if (!cy) return;
    cy.elements().removeClass('highlighted');
    var ids = p.hops.map(function (h) { return nodeId(h.name, h.version); });
    for (var i = 1; i < ids.length; i++) {
      cy.getElementById('e:' + ids[i - 1] + '>' + ids[i]).addClass('highlighted');
    }
    var nodesToFit = cy.nodes().filter(function (n) { return ids.indexOf(n.id()) !== -1; });
    if (nodesToFit.length) cy.animate({ fit: { eles: nodesToFit, padding: 60 }, duration: 400 });
  }

  function onNodeClick(node) {
    var name = node.data('name'), version = node.data('version'), flagged = node.data('flagged');
    if (flagged && currentData) {
      var p = (currentData.paths || []).filter(function (pp) { return pp.target.name === name && pp.target.version === version; })[0];
      if (p) { openInspectorForTarget(p); return; }
    }
    openInspectorForPlainNode(node, name, version);
  }

  function openInspectorForTarget(p) {
    $('ins-kind').textContent = 'Attack path target';
    $('ins-title').textContent = p.target.name + '@' + p.target.version;
    var factors = (p.score.factors || []).map(function (f) {
      return '<div class="factor"><div class="factor-top"><span>' + esc(f.label) + ' &middot; weight ' + f.weightPct + '%</span><span>' + f.value + '/100</span></div>' +
        '<div class="factor-bar"><span style="width:' + f.value + '%"></span></div>' +
        '<div class="factor-note">' + esc(f.note) + '</div></div>';
    }).join('');
    var chain = p.hops.map(function (h) { return h.name + (h.version ? ('@' + h.version) : ''); }).join(' &rarr; ');
    var findings = (p.findings || []).map(function (f) {
      return '<div class="finding-item">' + (f.url ? ('<a href="' + f.url + '" target="_blank" rel="noopener">' + esc(f.id) + '</a>') : esc(f.id)) +
        (f.severity ? (' &middot; ' + esc(f.severity)) : '') +
        (f.summary ? ('<p>' + esc(f.summary) + '</p>') : '') + '</div>';
    }).join('');
    $('ins-body').innerHTML =
      '<div class="ins-row"><div class="ins-label">Risk score</div><div class="ins-value" style="font-family:var(--mono);font-size:22px;color:var(' + severityVar(p.score.label) + ')">' + p.score.value + ' &middot; ' + p.score.label + '</div></div>' +
      '<div class="ins-row"><div class="ins-label">Path from scan subject</div><div class="ins-value mono" style="font-size:12px">' + chain + '</div></div>' +
      '<div class="ins-row"><div class="ins-label">Why this score</div>' + factors + '</div>' +
      '<div class="ins-row"><div class="ins-label">Findings (' + (p.findings || []).length + ')</div>' + findings + '</div>';
    openInspector();
  }

  function openInspectorForPlainNode(node, name, version) {
    $('ins-kind').textContent = node.data('root') ? 'Scan subject' : 'Package';
    $('ins-title').textContent = version ? (name + '@' + version) : name;
    var body = '<div class="ins-row"><div class="ins-label">Status</div><div class="ins-value">No findings on this package version.</div></div>';
    if (version) body += '<div class="ins-row"><button class="btn btn-primary" id="expand-btn" style="width:100%">Expand its dependencies</button></div>';
    $('ins-body').innerHTML = body;
    openInspector();
    if (version) $('expand-btn').addEventListener('click', function () { expandNode(name, version); });
  }

  function expandNode(name, version) {
    var btn = $('expand-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Expanding…'; }
    fetch('/api/expand', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ecosystem: 'npm', package: name, version: version }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!cy) return;
        var toAdd = [];
        (d.nodes || []).forEach(function (n) {
          var id = nodeId(n.name, n.version);
          if (cy.getElementById(id).length === 0) toAdd.push({ data: { id: id, name: n.name, version: n.version, flagged: false, root: false, severity: '' } });
        });
        (d.edges || []).forEach(function (e) {
          var eid = 'e:' + e.from + '>' + e.to;
          if (cy.getElementById(eid).length === 0) toAdd.push({ data: { id: eid, source: e.from, target: e.to } });
        });
        if (toAdd.length === 0) { showToast('No further direct dependencies to show.'); return; }
        cy.add(toAdd);
        cy.layout(graphLayout()).run();
        showToast('Expanded ' + name + ' — added ' + (toAdd.filter(function (t) { return t.data.name; }).length) + ' packages.');
        closeInspector();
      })
      .catch(function (err) { showToast('Expand failed: ' + err); });
  }

  // ---- inspector chrome ----
  function openInspector() { $('inspector').classList.add('open'); $('scrim').classList.add('open'); }
  function closeInspector() { $('inspector').classList.remove('open'); $('scrim').classList.remove('open'); }
  $('inspector-close').addEventListener('click', closeInspector);
  $('scrim').addEventListener('click', closeInspector);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeInspector(); });

  // Debug/test hook only — not used by the app itself. Lets a test click a
  // graph node at its real rendered position instead of guessing pixel
  // coordinates from layout, which shifts every scan.
  window.__reachgraph = { cy: function () { return cy; } };

  showView('overview');
})();
