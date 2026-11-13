/* Marks the page as scripted before it paints (site.js adds js-ready). */
(function (d) {
  var c = d.documentElement;
  c.className = c.className.replace(/(^|\s)no-js(\s|$)/, '$1js$2');
})(document);
