import CoreGraphics
import Foundation
import ImageIO
import PDFKit
import UniformTypeIdentifiers

func fail(_ message: String) -> Never {
    fputs("Error: \(message)\n", stderr)
    exit(1)
}

func parsePages(_ specification: String, pageCount: Int) -> [Int] {
    var pages = Set<Int>()

    for token in specification.split(separator: ",") {
        let parts = token.split(separator: "-", omittingEmptySubsequences: false)
        if parts.count == 1, let page = Int(parts[0]) {
            pages.insert(page)
        } else if parts.count == 2,
                  let start = Int(parts[0]),
                  let end = Int(parts[1]),
                  start <= end {
            pages.formUnion(start...end)
        } else {
            fail("Invalid page range '\(token)'.")
        }
    }

    let result = pages.sorted()
    if result.isEmpty || result.contains(where: { $0 < 1 || $0 > pageCount }) {
        fail("Page range must select pages from 1 through \(pageCount).")
    }
    return result
}

let isCountMode = CommandLine.arguments.count == 3 && CommandLine.arguments[2] == "--count"
let isTextMode = CommandLine.arguments.count == 5 && CommandLine.arguments[2] == "--extract-text"
guard isCountMode || CommandLine.arguments.count == 5 else {
    fail("Usage: render_pdf_pages.swift INPUT.pdf --count")
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let document = PDFDocument(url: inputURL) else {
    fail("Could not open PDF '\(inputURL.path)'.")
}

if isCountMode {
    print(document.pageCount)
    exit(0)
}

let outputPath = isTextMode ? CommandLine.arguments[3] : CommandLine.arguments[2]
let outputURL = URL(fileURLWithPath: outputPath, isDirectory: true)
let dpi: Double
if isTextMode {
    dpi = 0
} else {
    guard let parsedDpi = Double(CommandLine.arguments[3]), parsedDpi > 0 else {
        fail("DPI must be a positive number.")
    }
    dpi = parsedDpi
}
let selectedPages = parsePages(CommandLine.arguments[4], pageCount: document.pageCount)
do {
    try FileManager.default.createDirectory(
        at: outputURL,
        withIntermediateDirectories: true
    )
} catch {
    fail("Could not create output directory: \(error)")
}

let scale = dpi / 72.0
for pageNumber in selectedPages {
    guard let page = document.page(at: pageNumber - 1) else {
        fail("Could not read page \(pageNumber).")
    }

    if isTextMode {
        let textURL = outputURL.appendingPathComponent(
            String(format: "page_%03d.txt", pageNumber)
        )
        do {
            try (page.string ?? "").write(
                to: textURL,
                atomically: true,
                encoding: .utf8
            )
        } catch {
            fail("Could not write text file '\(textURL.path)': \(error)")
        }
        continue
    }

    let bounds = page.bounds(for: .cropBox)
    let width = max(1, Int(ceil(bounds.width * scale)))
    let height = max(1, Int(ceil(bounds.height * scale)))
    guard let context = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
    ) else {
        fail("Could not allocate a bitmap for page \(pageNumber).")
    }

    context.setFillColor(CGColor(gray: 1, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.saveGState()
    context.scaleBy(x: scale, y: scale)
    context.translateBy(x: -bounds.origin.x, y: -bounds.origin.y)
    page.draw(with: .cropBox, to: context)
    context.restoreGState()

    guard let image = context.makeImage() else {
        fail("Could not create an image for page \(pageNumber).")
    }
    let imageURL = outputURL.appendingPathComponent(
        String(format: "page_%03d.jpg", pageNumber)
    )
    guard let destination = CGImageDestinationCreateWithURL(
        imageURL as CFURL,
        UTType.jpeg.identifier as CFString,
        1,
        nil
    ) else {
        fail("Could not create image file '\(imageURL.path)'.")
    }
    CGImageDestinationAddImage(
        destination,
        image,
        [kCGImageDestinationLossyCompressionQuality: 0.9] as CFDictionary
    )
    if !CGImageDestinationFinalize(destination) {
        fail("Could not write image file '\(imageURL.path)'.")
    }
}

if isTextMode {
    print("Extracted text from \(selectedPages.count) page(s) to \(outputURL.path).")
} else {
    print("Rendered \(selectedPages.count) page(s) at \(Int(dpi)) DPI to \(outputURL.path).")
}
