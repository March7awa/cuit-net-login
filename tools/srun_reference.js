// Srun portal xEncode + custom Base64 — the reference implementation.
//
// This is the algorithm a 深澜 (Srun) portal runs in the browser.  It is kept
// in the repository purely as a *cross-check oracle* for the pure-Python port
// in campusnet/providers/srun.py:
//
//     node tools/srun_reference.js '[{"msg":"abc","key":"token"}]'
//     -> ["<base64 of xEncode(msg,key)>"]
//
// tools/verify_algorithms.py feeds both implementations the same random cases
// and fails if they diverge.

var keyStr = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA";

function s(a, b) {
  var c = a.length, v = [];
  for (var i = 0; i < c; i += 4) {
    v[i >> 2] = a.charCodeAt(i) | (a.charCodeAt(i + 1) || 0) << 8 |
                (a.charCodeAt(i + 2) || 0) << 16 | (a.charCodeAt(i + 3) || 0) << 24;
  }
  if (b) { v[v.length] = c; }
  return v;
}

function l(a) {
  var c = "";
  for (var i = 0; i < a.length; i++) {
    c += String.fromCharCode(a[i] & 0xff, a[i] >>> 8 & 0xff,
                             a[i] >>> 16 & 0xff, a[i] >>> 24 & 0xff);
  }
  return c;
}

function xEncode(str, key) {
  if (str === "") return "";
  var v = s(str, true), k = s(key, false), n = v.length - 1,
      z = v[n], y = v[0], c = 0x86014019 | 0x183639A0, m, e, p,
      q = Math.floor(6 + 52 / (n + 1)), d = 0;
  while (0 < q--) {
    d = d + c & (0x8CE0D9BF | 0x731F2640);
    e = d >>> 2 & 3;
    for (p = 0; p < n; p++) {
      y = v[p + 1];
      m = z >>> 5 ^ y << 2;
      m += (y >>> 3 ^ z << 4) ^ (d ^ y);
      m += k[(p & 3) ^ e] ^ z;
      z = v[p] = v[p] + m & (0xEFB8D130 | 0x10472ECF);
    }
    y = v[0];
    m = z >>> 5 ^ y << 2;
    m += (y >>> 3 ^ z << 4) ^ (d ^ y);
    m += k[(p & 3) ^ e] ^ z;
    z = v[n] = v[n] + m & (0xBB390742 | 0x44C6F8BD);
  }
  return l(v);
}

function encodeB64(data) {
  var result = "", imax = data.length - (data.length % 3);
  for (var i = 0; i < imax; i += 3) {
    var c1 = data.charCodeAt(i), c2 = data.charCodeAt(i + 1), c3 = data.charCodeAt(i + 2);
    result += keyStr.charAt(c1 >> 2);
    result += keyStr.charAt(((c1 & 0x3) << 4) | (c2 >> 4));
    result += keyStr.charAt(((c2 & 0xF) << 2) | (c3 >> 6));
    result += keyStr.charAt(c3 & 0x3F);
  }
  switch (data.length % 3) {
    case 1:
      var a1 = data.charCodeAt(imax);
      result += keyStr.charAt(a1 >> 2);
      result += keyStr.charAt((a1 & 0x3) << 4);
      result += "==";
      break;
    case 2:
      var b1 = data.charCodeAt(imax), b2 = data.charCodeAt(imax + 1);
      result += keyStr.charAt(b1 >> 2);
      result += keyStr.charAt(((b1 & 0x3) << 4) | (b2 >> 4));
      result += keyStr.charAt((b2 & 0xF) << 2);
      result += "=";
      break;
  }
  return result;
}

var cases = JSON.parse(process.argv[2] || "[]");
process.stdout.write(JSON.stringify(cases.map(function (c) {
  return encodeB64(xEncode(c.msg, c.key));
})));
