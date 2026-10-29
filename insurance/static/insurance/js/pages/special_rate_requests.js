
(function () {
  var buttons = document.querySelectorAll('.rate-type-btn');
  var sections = {
    mgbgSection: document.getElementById('mgbgSection'),
    bgSection: document.getElementById('bgSection')
  };

  buttons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      buttons.forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      var target = btn.getAttribute('data-target');
      Object.keys(sections).forEach(function (key) {
        sections[key].style.display = (key === target) ? '' : 'none';
      });
    });
  });
})();
