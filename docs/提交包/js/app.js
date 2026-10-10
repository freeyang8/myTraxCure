(function(){
  "use strict";
  var content = document.getElementById('content');
  var sections = Array.prototype.slice.call(content.querySelectorAll('section.mod'));
  var toc = document.getElementById('toc');
  var searchInput = document.getElementById('search');
  var hint = document.getElementById('searchHint');
  var empty = document.getElementById('empty');
  var totop = document.getElementById('totop');

  // ---- 统计 ----
  var classCount = content.querySelectorAll('h3.api-h').length;
  var methodCount = content.querySelectorAll('table.api tbody tr').length;
  var modCount = sections.length;
  var stats = document.getElementById('stats');
  if(stats){
    stats.innerHTML =
      '<div class="stat"><div class="num">' + modCount + '</div><div class="lbl">文档章节</div></div>' +
      '<div class="stat"><div class="num">' + classCount + '</div><div class="lbl">类 / 结构块</div></div>' +
      '<div class="stat"><div class="num">' + methodCount + '</div><div class="lbl">接口 / 字段条目</div></div>' +
      '<div class="stat"><div class="num">5151</div><div class="lbl">源码行数</div></div>';
  }

  // ---- 生成目录 ----
  var groups = [];
  var groupMap = {};
  sections.forEach(function(sec){
    var g = sec.getAttribute('data-group') || '其他';
    if(!groupMap[g]){ groupMap[g] = []; groups.push(g); }
    groupMap[g].push(sec);
  });
  var frag = document.createDocumentFragment();
  groups.forEach(function(g){
    var wrap = document.createElement('div');
    wrap.className = 'nav-group';
    var t = document.createElement('div');
    t.className = 'gtitle';
    t.textContent = g;
    wrap.appendChild(t);
    groupMap[g].forEach(function(sec){
      var a = document.createElement('a');
      a.href = '#' + sec.id;
      a.setAttribute('data-target', sec.id);
      a.textContent = sec.getAttribute('data-title') || sec.id;
      wrap.appendChild(a);
    });
    frag.appendChild(wrap);
  });
  toc.appendChild(frag);

  // ---- 缓存原始 HTML（搜索高亮可还原）----
  sections.forEach(function(sec){ sec._orig = sec.innerHTML; });

  // ---- 高亮 ----
  function clearHighlight(sec){
    if(sec._dirty){ sec.innerHTML = sec._orig; sec._dirty = false; }
  }
  function highlight(sec, q){
    var walker = document.createTreeWalker(sec, NodeFilter.SHOW_TEXT, {
      acceptNode: function(node){
        if(!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        var p = node.parentNode;
        if(p && (p.tagName === 'SCRIPT' || p.tagName === 'STYLE' || p.tagName === 'MARK')) return NodeFilter.FILTER_REJECT;
        return node.nodeValue.toLowerCase().indexOf(q) >= 0 ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    var nodes = [], n;
    while((n = walker.nextNode())) nodes.push(n);
    nodes.forEach(function(node){
      var text = node.nodeValue, low = text.toLowerCase();
      var f = document.createDocumentFragment(), i = 0;
      while(true){
        var idx = low.indexOf(q, i);
        if(idx < 0){ f.appendChild(document.createTextNode(text.slice(i))); break; }
        f.appendChild(document.createTextNode(text.slice(i, idx)));
        var m = document.createElement('mark');
        m.textContent = text.slice(idx, idx + q.length);
        f.appendChild(m);
        i = idx + q.length;
      }
      node.parentNode.replaceChild(f, node);
    });
  }

  // ---- 搜索 ----
  function doSearch(){
    var q = searchInput.value.trim().toLowerCase();
    var shown = 0;
    sections.forEach(function(sec){
      clearHighlight(sec);
      if(!q){ sec.style.display = ''; shown++; return; }
      var txt = sec.textContent.toLowerCase();
      if(txt.indexOf(q) >= 0){
        highlight(sec, q);
        sec._dirty = true;
        sec.style.display = '';
        shown++;
      } else {
        sec.style.display = 'none';
      }
    });
    // 过滤目录
    var links = toc.querySelectorAll('a');
    links.forEach(function(a){
      var target = document.getElementById(a.getAttribute('data-target'));
      a.style.display = (target && target.style.display === 'none') ? 'none' : '';
    });
    toc.querySelectorAll('.nav-group').forEach(function(grp){
      var any = Array.prototype.some.call(grp.querySelectorAll('a'), function(a){ return a.style.display !== 'none'; });
      grp.style.display = any ? '' : 'none';
    });
    empty.style.display = (q && shown === 0) ? 'block' : 'none';
    hint.textContent = q ? ('匹配 ' + shown + ' / ' + sections.length + ' 个章节') : '支持类名、方法名、字段名、中文说明';
  }
  searchInput.addEventListener('input', doSearch);
  searchInput.addEventListener('keydown', function(e){ if(e.key === 'Escape'){ searchInput.value=''; doSearch(); } });

  // ---- 滚动高亮目录 ----
  var links = Array.prototype.slice.call(toc.querySelectorAll('a'));
  function setActive(id){
    links.forEach(function(a){ a.classList.toggle('active', a.getAttribute('data-target') === id); });
  }
  if('IntersectionObserver' in window){
    var io = new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        if(en.isIntersecting) setActive(en.target.id);
      });
    }, { rootMargin: '-70px 0px -70% 0px', threshold: 0 });
    sections.forEach(function(sec){ io.observe(sec); });
  }

  // ---- 回到顶部 ----
  window.addEventListener('scroll', function(){
    totop.style.display = window.scrollY > 500 ? 'block' : 'none';
  }, { passive: true });
  totop.addEventListener('click', function(){ window.scrollTo({ top: 0, behavior: 'smooth' }); });

  // ---- 快捷键：/ 聚焦搜索 ----
  document.addEventListener('keydown', function(e){
    if(e.key === '/' && document.activeElement !== searchInput){
      e.preventDefault(); searchInput.focus();
    }
  });

  doSearch();
})();