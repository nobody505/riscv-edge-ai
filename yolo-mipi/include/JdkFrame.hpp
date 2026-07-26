#ifndef __JDK_FRAME_H__
#define __JDK_FRAME_H__
#include <cstdlib>	// For getenv
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "JdkDma.hpp"

// when passing between processes use scm rights to pass a file descriptor such as dma fd to another process
class JdkFrame {
public:
	using ReleaseCallback = std::function<void()>;
	JdkFrame(int dma_fd_, size_t size_, int w, int h);
	JdkFrame(unsigned char* external_ptr,
			 size_t size, int width, int height,
			 ReleaseCallback release_cb);
	~JdkFrame();
	// copy the dma buffer data to host memory and return
	unsigned char*			   toHost() const;
	std::vector<unsigned char> Clone() const;
	// manually release the mapping if needed
	// save as yuv file nv12 format
	bool   saveToFile(const std::string& filename) const;
	bool   loadFromFile(const std::string& filename, size_t expected_size);
	int	   getDMAFd() const;
	size_t getSize() const { return size_; }
	int	   getWidth() const { return width_; }
	int	   getHeight() const { return height_; }
	int	   MemCopy(const uint8_t* nalu, int nalu_size, int offset = 0);

private:
	size_t size_;  // total data size nv12 format width height 3 2
	int	   width_;
	int	   height_;
	///
	bool			zero_copy_	  = false;
	unsigned char*	external_ptr_ = nullptr;
	ReleaseCallback release_cb_;
	//
	JdkDma dma_;
	// sync
	std::shared_ptr<JdkDmaBuffer> data_;
};

using JdkFramePtr = std::shared_ptr<JdkFrame>;

#endif