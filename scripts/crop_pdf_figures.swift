import CoreGraphics
import Foundation
import ImageIO
import PDFKit
import UniformTypeIdentifiers

struct NormalizedBounds: Decodable {
    let left: Double
    let top: Double
    let right: Double
    let bottom: Double
}

struct FigureCoordinate: Decodable {
    let figure_id: String
    let page: Int
    let bbox: NormalizedBounds?
    let status: String?
}

struct FigureManifest: Decodable {
    let figures: [FigureCoordinate]
}

func fail(_ message: String) -> Never {
    fputs("Error: \(message)\n", stderr)
    exit(1)
}

func outputFilename(for figureID: String) -> String {
    let components = figureID.split(whereSeparator: \.isWhitespace)
    let safeName = String(components.joined(separator: "_").map { character in
            character.isLetter || character.isNumber || character == "-" || character == "_"
                ? character
                : "_"
        })
    return "\(safeName).png"
}

guard CommandLine.arguments.count == 5 else {
    fail("Usage: crop_pdf_figures.swift INPUT.pdf COORDINATES.json OUTPUT_DIR DPI")
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let manifestURL = URL(fileURLWithPath: CommandLine.arguments[2])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[3], isDirectory: true)
guard let dpi = Double(CommandLine.arguments[4]), dpi > 0 else {
    fail("DPI must be a positive number.")
}
guard let document = PDFDocument(url: inputURL) else {
    fail("Could not open PDF '\(inputURL.path)'.")
}

let manifest: FigureManifest
do {
    manifest = try JSONDecoder().decode(
        FigureManifest.self,
        from: Data(contentsOf: manifestURL)
    )
} catch {
    fail("Could not read coordinate manifest '\(manifestURL.path)': \(error)")
}
guard !manifest.figures.isEmpty else {
    fail("Coordinate manifest contains no figures.")
}

do {
    try FileManager.default.createDirectory(
        at: outputURL,
        withIntermediateDirectories: true
    )
} catch {
    fail("Could not create output directory '\(outputURL.path)': \(error)")
}

let scale = dpi / 72.0
var seenFilenames = Set<String>()
var renderedCount = 0

for figure in manifest.figures {
    if figure.status?.lowercased() == "unresolved" || figure.bbox == nil {
        print("Skipping unresolved \(figure.figure_id).")
        continue
    }
    guard let bbox = figure.bbox else {
        continue
    }
    guard figure.page >= 1, figure.page <= document.pageCount else {
        fail("\(figure.figure_id) selects page \(figure.page), outside the PDF.")
    }
    guard
        bbox.left.isFinite, bbox.top.isFinite,
        bbox.right.isFinite, bbox.bottom.isFinite,
        bbox.left >= 0, bbox.top >= 0,
        bbox.right <= 1, bbox.bottom <= 1,
        bbox.left < bbox.right, bbox.top < bbox.bottom
    else {
        fail("\(figure.figure_id) has invalid normalized crop bounds.")
    }

    let filename = outputFilename(for: figure.figure_id)
    guard seenFilenames.insert(filename).inserted else {
        fail("Coordinate manifest contains duplicate output name '\(filename)'.")
    }
    guard let page = document.page(at: figure.page - 1) else {
        fail("Could not read PDF page \(figure.page) for \(figure.figure_id).")
    }

    let pageBounds = page.bounds(for: .cropBox)
    let cropRect = CGRect(
        x: pageBounds.minX + bbox.left * pageBounds.width,
        y: pageBounds.minY + (1 - bbox.bottom) * pageBounds.height,
        width: (bbox.right - bbox.left) * pageBounds.width,
        height: (bbox.bottom - bbox.top) * pageBounds.height
    )
    let pixelWidth = max(1, Int(ceil(cropRect.width * scale)))
    let pixelHeight = max(1, Int(ceil(cropRect.height * scale)))
    guard let context = CGContext(
        data: nil,
        width: pixelWidth,
        height: pixelHeight,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
    ) else {
        fail("Could not allocate a bitmap for \(figure.figure_id).")
    }

    context.setFillColor(CGColor(gray: 1, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: pixelWidth, height: pixelHeight))
    context.saveGState()
    context.scaleBy(x: scale, y: scale)
    context.translateBy(x: -cropRect.minX, y: -cropRect.minY)
    page.draw(with: .cropBox, to: context)
    context.restoreGState()

    guard let image = context.makeImage() else {
        fail("Could not create a bitmap for \(figure.figure_id).")
    }
    let imageURL = outputURL.appendingPathComponent(filename)
    guard let destination = CGImageDestinationCreateWithURL(
        imageURL as CFURL,
        UTType.png.identifier as CFString,
        1,
        nil
    ) else {
        fail("Could not create image '\(imageURL.path)'.")
    }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else {
        fail("Could not write image '\(imageURL.path)'.")
    }

    renderedCount += 1
    print("Cropped \(figure.figure_id) from page \(figure.page) to \(imageURL.path).")
}

guard renderedCount > 0 else {
    fail("No resolved figures were rendered.")
}
print("Rendered \(renderedCount) figure(s) at \(Int(dpi)) DPI.")
