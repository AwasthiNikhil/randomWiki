// Counts the letters and digits of readable text in each image given (macOS Vision). One number per line.
// rw.py compiles this once into .tools/ocr and uses it to skip clips with captions or channel stamps burned in.
import AppKit
import Vision

for path in CommandLine.arguments.dropFirst() {
    var n = 0
    if let img = NSImage(contentsOfFile: path), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) {
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = .accurate
        req.automaticallyDetectsLanguage = true
        try? VNImageRequestHandler(cgImage: cg).perform([req])
        for o in req.results ?? [] {
            if let t = o.topCandidates(1).first, t.confidence > 0.5, o.boundingBox.height > 0.012 {
                n += t.string.filter { $0.isLetter || $0.isNumber }.count
            }
        }
    }
    print(n)
}
