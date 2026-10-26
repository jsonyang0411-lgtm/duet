// 桌面微信 OCR 小工具
// 用法：ocr wid            -> 打印微信主窗口 windowID
//      ocr <图片路径>      -> 输出 JSON 行：文字、相对坐标（左上角原点）、文字左侧气泡底色
import Cocoa
import Vision

let args = CommandLine.arguments
if args.count > 1 && args[1] == "wid" {
  let wins = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as! [[String: Any]]
  for w in wins where ["微信", "WeChat"].contains(w[kCGWindowOwnerName as String] as? String ?? "")
    && (w[kCGWindowLayer as String] as? Int ?? 1) == 0
    && ((w[kCGWindowBounds as String] as? [String: Any])?["Width"] as? Int ?? 0) > 300 {
    print(w[kCGWindowNumber as String]!); exit(0)
  }
  exit(1)
}

let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[1]) as CFURL, nil)!
let img = CGImageSourceCreateImageAtIndex(src, 0, nil)!
let rep = NSBitmapImageRep(cgImage: img)
let W = Double(img.width), H = Double(img.height)

// 取文字框左侧 8 像素处的颜色，用来区分绿色（自己）和灰色（别人）气泡
func color(_ x: Double, _ y: Double) -> [Int] {
  let px = max(0, min(Int(x), img.width - 1)), py = max(0, min(Int(y), img.height - 1))
  guard let c = rep.colorAt(x: px, y: py)?.usingColorSpace(.deviceRGB) else { return [0, 0, 0] }
  return [Int(c.redComponent * 255), Int(c.greenComponent * 255), Int(c.blueComponent * 255)]
}

let req = VNRecognizeTextRequest()
req.recognitionLanguages = ["zh-Hans", "en-US"]
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false
try? VNImageRequestHandler(cgImage: img).perform([req])
for o in req.results ?? [] {
  guard let s = o.topCandidates(1).first?.string else { continue }
  let b = o.boundingBox
  // Double, not CGFloat: older Swift toolchains find "(y0 + y1) / 2 * H" ambiguous otherwise
  let x0 = Double(b.minX), x1 = Double(b.maxX), y0 = 1 - Double(b.maxY), y1 = 1 - Double(b.minY)
  let row: [String: Any] = ["t": s, "x0": x0, "x1": x1, "y0": y0, "y1": y1,
                            "bg": color(x0 * W - 8, (y0 + y1) / 2 * H)]
  let d = try! JSONSerialization.data(withJSONObject: row)
  print(String(data: d, encoding: .utf8)!)
}
