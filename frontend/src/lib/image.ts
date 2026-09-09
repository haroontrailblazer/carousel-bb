/**
 * Compress an image in the browser before it is uploaded.
 *
 * A phone camera photo is 3-8 MB and an avatar is drawn at 56 pixels. Sending
 * the original would waste the upload on a slow connection, the storage, and
 * every page load afterwards - and none of it buys a single visible pixel.
 * So the browser does the work: it already has the decoder and the canvas,
 * and doing it here means the big file never leaves the device at all.
 *
 * WebP because every browser this app supports can encode it, and it is
 * roughly a third the size of JPEG at the same visual quality. The result is
 * typically 10-25 KB.
 */

/** Longest edge of the stored image. Generous for a 56px display, so the
 *  picture still looks right on a retina screen and in the larger profile
 *  preview. */
const MAX_EDGE = 512

/** Encoder quality. Above ~0.85 the file grows fast for no visible gain. */
const QUALITY = 0.82

export type CompressedImage = {
  blob: Blob
  width: number
  height: number
  /** For an <img> preview before the upload finishes. */
  objectUrl: string
}

/** Read a File into an HTMLImageElement, honouring EXIF orientation. */
async function decode(file: File): Promise<ImageBitmap | HTMLImageElement> {
  // createImageBitmap applies EXIF orientation with imageOrientation:"from-image",
  // which is what stops portrait phone photos arriving on their side.
  if (typeof createImageBitmap === "function") {
    try {
      return await createImageBitmap(file, { imageOrientation: "from-image" })
    } catch {
      /* fall through to the <img> path */
    }
  }
  const url = URL.createObjectURL(file)
  try {
    const image = new Image()
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve()
      image.onerror = () => reject(new Error("That file is not an image."))
      image.src = url
    })
    return image
  } finally {
    // The decoded pixels are retained by the element; the URL is not needed.
    URL.revokeObjectURL(url)
  }
}

/**
 * Square-crop to the centre, scale down, and encode as WebP.
 *
 * Cropping here rather than with CSS means the stored file IS the avatar:
 * no oversized rectangle carried around to be masked by every consumer.
 */
export async function compressAvatar(file: File): Promise<CompressedImage> {
  if (!file.type.startsWith("image/")) {
    throw new Error("Choose an image file.")
  }

  const source = await decode(file)
  const sourceWidth = "width" in source ? source.width : 0
  const sourceHeight = "height" in source ? source.height : 0
  if (!sourceWidth || !sourceHeight) {
    throw new Error("That image could not be read.")
  }

  const edge = Math.min(sourceWidth, sourceHeight)
  const size = Math.min(edge, MAX_EDGE)
  const canvas = document.createElement("canvas")
  canvas.width = size
  canvas.height = size

  const context = canvas.getContext("2d")
  if (!context) throw new Error("This browser cannot process images.")
  context.imageSmoothingQuality = "high"
  context.drawImage(
    source as CanvasImageSource,
    (sourceWidth - edge) / 2,
    (sourceHeight - edge) / 2,
    edge,
    edge,
    0,
    0,
    size,
    size,
  )
  if ("close" in source && typeof source.close === "function") source.close()

  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, "image/webp", QUALITY),
  )
  if (!blob) throw new Error("Could not compress that image.")

  return {
    blob,
    width: size,
    height: size,
    objectUrl: URL.createObjectURL(blob),
  }
}
