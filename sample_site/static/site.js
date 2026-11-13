/* Picking UX on top of the plain form (ES5, no frameworks).
   The real form data are the hidden checkboxes name="pick"; everything here
   only toggles them, so the page still submits when this file fails. */
(function () {
  'use strict';

  var MSG = {
    cap: '最多选 {n} 顶，可以先取消一顶再选',
    none: '请先选至少一顶',
    needSize: '请先选尺码',
    pick: '选这顶',
    picked: '已选 ✓（点此取消）',
    chosen: '已选：',
    badge: '已选',
    filled: '已帮您填好，请核对一下',
    notFound: '没认出这段地址，请直接填写下面几项'
  };

  var doc = document;
  var root = doc.documentElement;

  function hasClass(el, c) { return (' ' + el.className + ' ').indexOf(' ' + c + ' ') >= 0; }
  function addClass(el, c) { if (!hasClass(el, c)) { el.className = (el.className ? el.className + ' ' : '') + c; } }
  function removeClass(el, c) {
    el.className = (' ' + el.className + ' ').replace(' ' + c + ' ', ' ').replace(/^\s+|\s+$/g, '');
  }
  function setClass(el, c, on) { if (on) { addClass(el, c); } else { removeClass(el, c); } }
  function each(list, fn) { for (var i = 0; i < list.length; i++) { fn(list[i], i); } }
  function qs(sel, el) { return (el || doc).querySelector(sel); }
  function qsa(sel, el) { return (el || doc).querySelectorAll(sel); }
  function on(el, ev, fn) { if (el) { el.addEventListener(ev, fn, false); } }
  function text(el, value) { if (el) { el.textContent = value; } }

  var toastTimer = null;
  function toast(message) {
    var el = qs('[data-toast]');
    if (!el) { return; }
    el.textContent = message;
    addClass(el, 'is-on');
    if (toastTimer) { clearTimeout(toastTimer); }
    toastTimer = setTimeout(function () { removeClass(el, 'is-on'); }, 2200);
  }

  /* ---------------------------------------------------------------- picker */

  function Card(el) {
    this.el = el;
    this.img = qs('.ph-img', el);
    this.badge = qs('[data-badge]', el);
    this.chips = qsa('[data-colours] .chip', el);
    this.sizesBox = qs('[data-sizes]', el);
    this.chosenLine = qs('[data-chosen]', el);
    this.pickBtn = qs('[data-pick]', el);
    this.boxes = qsa('input[name=pick]', el);
    this.name = (qs('.nm', el) || {}).textContent || '';
    this.ci = parseInt(el.getAttribute('data-first'), 10) || 0;
    this.size = null;
  }

  Card.prototype.boxesOf = function (ci) {
    var out = [];
    each(this.boxes, function (b) {
      if (parseInt(b.getAttribute('data-ci'), 10) === ci) { out.push(b); }
    });
    return out;
  };

  Card.prototype.checkedOf = function (ci) {
    var boxes = this.boxesOf(ci);
    for (var i = 0; i < boxes.length; i++) { if (boxes[i].checked) { return boxes[i]; } }
    return null;
  };

  Card.prototype.picked = function () {
    var out = [];
    each(this.boxes, function (b) { if (b.checked) { out.push(b); } });
    return out;
  };

  Card.prototype.colourName = function (ci) {
    return this.chips[ci] ? this.chips[ci].textContent : '';
  };

  Card.prototype.render = function () {
    var self = this;
    var ci = this.ci;
    each(this.chips, function (chip, i) {
      setClass(chip, 'is-on', i === ci);
      setClass(chip, 'is-picked', !!self.checkedOf(i));
    });
    var chip = this.chips[ci];
    if (chip && this.img) {
      var src = chip.getAttribute('data-img');
      if (this.img.getAttribute('src') !== src) {
        this.img.setAttribute('src', src);
        this.img.setAttribute('width', chip.getAttribute('data-w'));
        this.img.setAttribute('height', chip.getAttribute('data-h'));
        this.img.setAttribute('alt', this.name + ' ' + chip.textContent);
      }
    }
    var boxes = this.boxesOf(ci);
    var current = this.checkedOf(ci);
    this.sizesBox.innerHTML = '';
    if (boxes.length > 1) {
      each(boxes, function (b) {
        var s = doc.createElement('button');
        s.type = 'button';
        s.className = 'chip';
        var label = b.getAttribute('data-size');
        s.textContent = label;
        var lit = current ? current === b : self.size === label;
        setClass(s, 'is-on', lit);
        on(s, 'click', function () { self.choseSize(b, label); });
        self.sizesBox.appendChild(s);
      });
    }
    setClass(this.pickBtn, 'is-on', !!current);
    text(this.pickBtn, current ? MSG.picked : MSG.pick);
    var picked = this.picked();
    var names = [];
    each(picked, function (b) {
      var c = self.colourName(parseInt(b.getAttribute('data-ci'), 10));
      names.push(b.getAttribute('data-multi') === '1' ? c + ' ' + b.getAttribute('data-size') : c);
    });
    text(this.chosenLine, names.length ? MSG.chosen + names.join('、') : '');
    setClass(this.el, 'is-picked', picked.length > 0);
    text(this.badge, picked.length ? MSG.badge + (picked.length > 1 ? ' ' + picked.length : '') : '');
  };

  Card.prototype.choseSize = function (box, label) {
    var current = this.checkedOf(this.ci);
    if (current && current !== box) {
      current.checked = false;
      box.checked = true;
    } else if (!current) {
      this.size = label;
    }
    removeClass(this.sizesBox, 'is-warn');
    this.render();
    Picker.update();
  };

  var Picker = {
    form: null,
    cards: [],
    max: 1,
    pushed: false,

    init: function (form) {
      this.form = form;
      this.max = parseInt(form.getAttribute('data-max'), 10) || 1;
      var self = this;
      each(qsa('[data-card]', form), function (el) {
        var card = new Card(el);
        self.cards.push(card);
        each(card.chips, function (chip, i) {
          on(chip, 'click', function () { card.ci = i; card.size = null; card.render(); });
        });
        on(card.pickBtn, 'click', function () { self.toggle(card); });
        card.render();
      });
      on(qs('[data-next]'), 'click', function () { self.next(); });
      on(qs('[data-back]', form), 'click', function () { self.back(); });
      on(form, 'submit', function (ev) {
        if (self.count() === 0) {
          ev.preventDefault();
          self.show('pick');
          toast(MSG.none);
        }
      });
      on(window, 'hashchange', function () { self.fromHash(); });
      if (form.getAttribute('data-start') === 'details' && this.count() > 0) {
        this.show('details');
      } else {
        this.fromHash();
      }
      this.update();
    },

    count: function () {
      var n = 0;
      each(this.cards, function (c) { n += c.picked().length; });
      return n;
    },

    toggle: function (card) {
      var current = card.checkedOf(card.ci);
      if (current) {
        current.checked = false;
      } else {
        var boxes = card.boxesOf(card.ci);
        var target = null;
        if (boxes.length === 1) {
          target = boxes[0];
        } else {
          for (var i = 0; i < boxes.length; i++) {
            if (boxes[i].getAttribute('data-size') === card.size) { target = boxes[i]; }
          }
        }
        if (!target) {
          addClass(card.sizesBox, 'is-warn');
          toast(MSG.needSize);
          return;
        }
        if (this.count() >= this.max) {
          toast(MSG.cap.replace('{n}', this.max));
          return;
        }
        target.checked = true;
      }
      card.render();
      this.update();
    },

    update: function () {
      var n = this.count();
      text(qs('[data-count]'), String(n));
      var next = qs('[data-next]');
      if (next) { setClass(next, 'is-off', n === 0); }
    },

    next: function () {
      if (this.count() === 0) { toast(MSG.none); return; }
      if (location.hash === '#details') { this.show('details'); return; }
      this.pushed = true;
      location.hash = 'details';
    },

    back: function () {
      if (this.pushed && location.hash === '#details') {
        this.pushed = false;
        history.back();
        return;
      }
      this.show('pick');
      if (window.history && history.replaceState) {
        history.replaceState(null, '', location.pathname + location.search);
      }
    },

    fromHash: function () {
      this.show(location.hash === '#details' && this.count() > 0 ? 'details' : 'pick');
    },

    show: function (step) {
      var details = step === 'details';
      setClass(this.form, 'on-details', details);
      setClass(doc.body, 'is-details-step', details);
      var bar = qs('#bar');
      if (bar) { setClass(bar, 'is-hidden', details); }
      if (details) { this.fillPicked(); }
      window.scrollTo(0, 0);
    },

    fillPicked: function () {
      var list = qs('[data-picked]', this.form);
      if (!list) { return; }
      list.innerHTML = '';
      each(this.cards, function (card) {
        each(card.picked(), function (b) {
          var ci = parseInt(b.getAttribute('data-ci'), 10);
          var chip = card.chips[ci];
          var li = doc.createElement('li');
          var img = doc.createElement('img');
          img.className = 'thumb';
          img.setAttribute('src', chip.getAttribute('data-img'));
          img.setAttribute('width', chip.getAttribute('data-w'));
          img.setAttribute('height', chip.getAttribute('data-h'));
          img.setAttribute('alt', card.name);
          var box = doc.createElement('div');
          box.className = 'it';
          var nm = doc.createElement('b');
          nm.textContent = card.name;
          var sub = doc.createElement('span');
          sub.textContent = chip.textContent + ' · ' + b.getAttribute('data-size');
          box.appendChild(nm);
          box.appendChild(sub);
          li.appendChild(img);
          li.appendChild(box);
          list.appendChild(li);
        });
      });
    }
  };

  /* ------------------------------------------------------- address paste */

  var PROVS = [
    ['北京市', '北京'], ['天津市', '天津'], ['上海市', '上海'], ['重庆市', '重庆'],
    ['河北省', '河北'], ['山西省', '山西'], ['辽宁省', '辽宁'], ['吉林省', '吉林'],
    ['黑龙江省', '黑龙江'], ['江苏省', '江苏'], ['浙江省', '浙江'], ['安徽省', '安徽'],
    ['福建省', '福建'], ['江西省', '江西'], ['山东省', '山东'], ['河南省', '河南'],
    ['湖北省', '湖北'], ['湖南省', '湖南'], ['广东省', '广东'], ['海南省', '海南'],
    ['四川省', '四川'], ['贵州省', '贵州'], ['云南省', '云南'], ['陕西省', '陕西'],
    ['甘肃省', '甘肃'], ['青海省', '青海'], ['台湾省', '台湾'],
    ['内蒙古自治区', '内蒙古'], ['广西壮族自治区', '广西'], ['西藏自治区', '西藏'],
    ['宁夏回族自治区', '宁夏'], ['新疆维吾尔自治区', '新疆'],
    ['香港特别行政区', '香港'], ['澳门特别行政区', '澳门']
  ];
  var MUNI = { '北京市': 1, '天津市': 1, '上海市': 1, '重庆市': 1 };
  var SEP = /^[\s,，;；:：、\/|]+|[\s,，;；:：、\/|]+$/g;

  function trimSep(s) { return String(s || '').replace(SEP, ''); }

  function parseAddress(raw) {
    var out = { name: '', phone: '', province: '', city: '', district: '', address: '' };
    var t = String(raw || '').replace(/[\r\n\t]+/g, ' ');
    t = t.replace(/(收货人|收件人|联系人|姓名|手机号码|手机号|手机|联系电话|电话|所在地区|所在地|详细地址|收货地址|地址)\s*[:：]?/g, ' ');
    t = t.replace(/中国(大陆)?/g, ' ');
    var m = t.match(/(?:\+?86[\s\-]?)?(1[3-9]\d)[\s\-]?(\d{4})[\s\-]?(\d{4})(?!\d)/);
    if (m) {
      out.phone = m[1] + m[2] + m[3];
      t = t.replace(m[0], ' ');
    }
    var best = -1;
    var prov = null;
    var used = '';
    var i;
    for (i = 0; i < PROVS.length; i++) {
      var at = t.indexOf(PROVS[i][0]);
      if (at >= 0 && (best < 0 || at < best)) { best = at; prov = PROVS[i]; used = PROVS[i][0]; }
    }
    if (!prov) {
      for (i = 0; i < PROVS.length; i++) {
        var at2 = t.indexOf(PROVS[i][1]);
        if (at2 >= 0 && (best < 0 || at2 < best)) { best = at2; prov = PROVS[i]; used = PROVS[i][1]; }
      }
    }
    if (!prov) { return out; }
    var before = t.slice(0, best);
    var rest = t.slice(best + used.length);
    if (used === prov[1]) {
      rest = rest.replace(/^(省|市|壮族自治区|回族自治区|维吾尔自治区|自治区|特别行政区)/, '');
    }
    rest = trimSep(rest);
    out.province = prov[0];
    if (MUNI[prov[0]]) {
      out.city = prov[0];
      rest = trimSep(rest.replace(new RegExp('^' + prov[1] + '市?'), '').replace(/^市辖区/, ''));
    } else if (prov[0].indexOf('特别行政区') < 0) {
      var cm = rest.match(/^([^\s,，\d]{1,12}?(?:自治州|地区|盟|市))/);
      if (cm) {
        out.city = cm[1];
        rest = trimSep(rest.slice(cm[0].length));
      }
    }
    if (out.city) {
      var dm = rest.match(/^([^\s,，\d]{1,8}?(?:自治县|自治旗|新区|区|县|旗|市))/);
      if (dm) {
        out.district = dm[1];
        rest = trimSep(rest.slice(dm[0].length));
      }
    }
    var nm = trimSep(before);
    if (nm && nm.length <= 12) {
      out.name = nm;
    } else {
      var tail = rest.match(/^(.*\S)\s+([一-龥]{2,4})$/);
      if (tail) { out.name = tail[2]; rest = tail[1]; }
    }
    out.address = trimSep(rest.replace(/\s+/g, ' '));
    return out;
  }

  function field(form, name) { return form.elements ? form.elements[name] : null; }

  function setProvince(select, value) {
    if (!select || !value) { return false; }
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === value) { select.selectedIndex = i; return true; }
    }
    return false;
  }

  function applyAddress(form, parsed, overwrite) {
    var changed = false;
    var names = ['name', 'phone', 'city', 'district', 'address'];
    for (var i = 0; i < names.length; i++) {
      var el = field(form, names[i]);
      var value = parsed[names[i]];
      if (el && value && (overwrite || !el.value)) { el.value = value; changed = true; }
    }
    var sel = field(form, 'province');
    if (sel && parsed.province && (overwrite || !sel.value)) {
      changed = setProvince(sel, parsed.province) || changed;
    }
    return changed;
  }

  function initPaste(form) {
    var box = qs('#paste', form);
    var go = qs('[data-paste]', form);
    var msg = qs('[data-paste-msg]', form);
    function run() {
      if (!box || !box.value) { return; }
      var parsed = parseAddress(box.value);
      var ok = parsed.province && applyAddress(form, parsed, true);
      text(msg, ok ? MSG.filled : MSG.notFound);
    }
    on(go, 'click', run);
    on(box, 'paste', function () { setTimeout(run, 0); });
    var addr = field(form, 'address');
    on(addr, 'paste', function () {
      setTimeout(function () {
        var sel = field(form, 'province');
        if (!addr.value || (sel && sel.value)) { return; }
        var parsed = parseAddress(addr.value);
        if (parsed.province && parsed.address) {
          addr.value = '';
          applyAddress(form, parsed, false);
        }
      }, 0);
    });
  }

  /* ------------------------------------------------------- confirm page */

  function initOnce(form) {
    var sent = false;
    on(form, 'submit', function (ev) {
      if (sent) { ev.preventDefault(); return; }
      sent = true;
      each(qsa('button', form), function (b) { addClass(b, 'is-off'); });
    });
  }

  function start() {
    var form = qs('#pickform');
    if (form) {
      Picker.init(form);
      initPaste(form);
    }
    each(qsa('form[data-once]'), initOnce);
    addClass(root, 'js-ready');
  }

  window.SampleSite = { parseAddress: parseAddress };

  try {
    start();
  } catch (err) {
    removeClass(root, 'js-ready');
    removeClass(root, 'js');
  }
})();
