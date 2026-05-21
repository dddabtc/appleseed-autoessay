/* ============================================================
 *  main.js —— 仅做最小交互，无依赖
 * ============================================================ */

(function () {
  'use strict';

  // ---------- 1. 汉堡菜单 ----------
  var hamburger = document.querySelector('.hamburger');
  var mobileNav = document.getElementById('mobile-nav');

  if (hamburger && mobileNav) {
    hamburger.addEventListener('click', function () {
      var isOpen = hamburger.getAttribute('aria-expanded') === 'true';
      hamburger.setAttribute('aria-expanded', String(!isOpen));
      if (isOpen) {
        mobileNav.setAttribute('hidden', '');
      } else {
        mobileNav.removeAttribute('hidden');
      }
    });
  }

  // ---------- 2. 密码显隐 ----------
  var toggles = document.querySelectorAll('[data-toggle-password]');
  toggles.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var field = btn.closest('.field');
      if (!field) return;
      var input = field.querySelector('input');
      if (!input) return;
      var isPwd = input.getAttribute('type') === 'password';
      input.setAttribute('type', isPwd ? 'text' : 'password');
      btn.classList.toggle('is-on', isPwd);
      btn.setAttribute('aria-label', isPwd ? '隐藏密码' : '显示密码');
    });
  });

  // ---------- 3. 表单提交占位 ----------
  var form = document.querySelector('.login-form');
  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var data = new FormData(form);
      console.log('[demo] sign-in submit:', Object.fromEntries(data.entries()));
      // 这里接入实际登录请求
    });
  }

  // ---------- 4. 语言切换占位 ----------
  var langBtn = document.querySelector('.lang-switch');
  if (langBtn) {
    langBtn.addEventListener('click', function () {
      console.log('[demo] open language menu');
      // 这里接入语言菜单
    });
  }
})();
